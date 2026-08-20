"""Episode、Profile 与可选向量记忆的唯一业务访问边界。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from models.contracts import DecisionType, Episode, MemoryContext, ProfileMemory

from .repositories import EpisodeRepository, ProfileRepository
from .vector_index import tokenize


class MemoryManager:
    """协调权威 SQLite 记忆与可失败、可重建的派生向量索引。"""

    def __init__(self, episodes: EpisodeRepository, profiles: ProfileRepository, index: Any) -> None:
        self.episodes = episodes
        self.profiles = profiles
        self.index = index
        self.last_index_error: Exception | None = None

    def write_episode(self, episode: Episode) -> Episode:
        """先持久化 Episode，再更新可重建索引并执行偏好反思。"""
        stored = self.episodes.save(episode)
        try:
            self.index.upsert(stored)
        except Exception as error:
            self.last_index_error = error
        self._reflect_profile_signals(stored)
        return stored

    def archive_completed(self, episode: Episode) -> Episode:
        return self.write_episode(episode)

    def retrieve_episodes(self, query: str, decision_type: DecisionType | str, limit: int = 3) -> list[Episode]:
        wanted = self._decision_type(decision_type)
        cap = min(limit, 10)
        # SQLite 是权威原文；Qdrant 只负责召回候选 ID，因此索引异常或陈旧都可由 SQLite 补足。
        try:
            identifiers = self.index.search(query, wanted, cap)
            selected: list[Episode] = []
            selected_ids: set[str] = set()
            for identifier in identifiers:
                item = self.episodes.get(identifier)
                if item is not None and item.decision_type == wanted and item.episode_id not in selected_ids:
                    selected.append(item)
                    selected_ids.add(item.episode_id)
                if len(selected) == cap:
                    return selected
            for item in self._sqlite_rank(query, wanted, cap):
                if item.episode_id not in selected_ids:
                    selected.append(item)
                    selected_ids.add(item.episode_id)
                if len(selected) == cap:
                    break
            return selected
        except Exception:
            return self._sqlite_rank(query, wanted, cap)

    def context_for(self, query: str, decision_type: DecisionType | str) -> MemoryContext:
        wanted = self._decision_type(decision_type)
        episodes = self.retrieve_episodes(query, wanted)
        profiles = [profile for profile in self.profiles.list() if profile.category == wanted.value]
        return MemoryContext(profile_memories=profiles, episodes=episodes, relevant_facts=[episode.summary for episode in episodes])

    def context_for_any(self, query: str, limit: int = 10) -> MemoryContext:
        """在总控尚未决定领域前，为模型提供跨领域但数量受限的相关历史上下文。"""
        query_tokens = set(tokenize(query))
        ranked = sorted(
            self.episodes.list(),
            key=lambda item: (len(query_tokens & set(tokenize(item.summary))), item.created_at),
            reverse=True,
        )[:min(limit, 10)]
        return MemoryContext(profile_memories=self.profiles.list(), episodes=ranked,
                             relevant_facts=[item.summary for item in ranked])

    def profile_for(self, category: str, memory_key: str) -> ProfileMemory | None:
        return next((item for item in self.profiles.list() if item.category == category and item.memory_key == memory_key), None)

    def record_feedback(self, episode: Episode) -> Episode:
        return self.write_episode(episode)

    def delete_decisions(self, archives, decision_ids: list[str], *, delete_memories: bool) -> list[str]:
        """协调归档删除和可失败的向量索引清理，不让索引故障阻断 SQLite 删除。"""
        episode_ids = archives.delete_decisions(decision_ids, delete_memories=delete_memories)
        if episode_ids:
            try:
                self.index.delete(episode_ids)
            except Exception as error:
                self.last_index_error = error
        return episode_ids

    def _sqlite_rank(self, query: str, decision_type: DecisionType, limit: int) -> list[Episode]:
        query_tokens = set(tokenize(query))
        now = datetime.now(timezone.utc)

        def score(item: Episode) -> tuple[float, datetime]:
            text_tokens = set(tokenize(item.summary))
            overlap = len(query_tokens & text_tokens)
            tag_overlap = len(query_tokens & set(tokenize(" ".join(item.tags))))
            age_days = max((now - item.created_at).total_seconds() / 86400, 0)
            return overlap * 10 + tag_overlap * 5 + 1 / (1 + age_days), item.created_at

        candidates = [item for item in self.episodes.list() if item.decision_type == decision_type]
        return sorted(candidates, key=score, reverse=True)[:limit]

    def _reflect_profile_signals(self, episode: Episode) -> None:
        # 显式信号立即可信；推断信号必须由至少两个不同 Episode 交叉印证，避免一次选择被固化为画像。
        for raw_signal in episode.profile_signals:
            parsed = self._parse_signal(raw_signal)
            if parsed is None:
                continue
            kind, category, key, value = parsed
            existing = self.profile_for(category, key)
            if kind == "explicit":
                self._save_profile(category, key, value, episode.episode_id, existing, replace=True)
                continue
            matching = self._matching_inferred_episode_ids(category, key, value)
            if existing is None:
                if len(matching) >= 2:
                    self._save_profile(category, key, value, episode.episode_id, None, replace=False, sources=matching)
            elif existing.value == value:
                self._save_profile(category, key, value, episode.episode_id, existing, replace=False, sources=matching)
            else:
                self._save_profile(category, key, existing.value, episode.episode_id, existing, replace=False, confidence=max(0.0, existing.confidence - 0.2))

    def _save_profile(self, category: str, key: str, value: Any, episode_id: str, existing: ProfileMemory | None, *, replace: bool, sources: list[str] | None = None, confidence: float | None = None) -> ProfileMemory:
        source_ids = list(dict.fromkeys([*(existing.source_episode_ids if existing else []), *(sources or []), episode_id]))
        return self.profiles.save(ProfileMemory(
            memory_id=existing.memory_id if existing else str(uuid4()), category=category, memory_key=key,
            value=value if replace or existing is None else existing.value,
            importance=existing.importance if existing else 0.7,
            confidence=confidence if confidence is not None else (1.0 if replace else min(1.0, (existing.confidence if existing else 0.5) + 0.1)),
            source_episode_ids=source_ids,
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
        ))

    def _matching_inferred_episode_ids(self, category: str, key: str, value: Any) -> list[str]:
        matching: list[str] = []
        for item in sorted(self.episodes.list(), key=lambda episode: (episode.created_at, episode.episode_id)):
            for signal in item.profile_signals:
                parsed = self._parse_signal(signal)
                if parsed == ("inferred", category, key, value):
                    matching.append(item.episode_id)
        return list(dict.fromkeys(matching))

    @staticmethod
    def _decision_type(value: DecisionType | str) -> DecisionType:
        return value if isinstance(value, DecisionType) else DecisionType(value)

    @staticmethod
    def _parse_signal(signal: str) -> tuple[str, str, str, Any] | None:
        try:
            kind, category, assignment = signal.split(":", 2)
            key, raw_value = assignment.split("=", 1)
            if kind not in {"explicit", "inferred"} or not category or not key:
                return None
            return kind, category, key, json.loads(raw_value.lower()) if raw_value.lower() in {"true", "false", "null"} else raw_value
        except ValueError:
            return None
