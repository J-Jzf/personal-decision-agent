"""由本地 Qdrant 支撑的嵌入式、确定性 Episode 相似度检索。"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from models.contracts import DecisionType, Episode


EMBEDDING_DIMENSION = 128
COLLECTION_NAME = "episodes"
_TOKEN_PATTERN = re.compile(r"[\w]+|[\u4e00-\u9fff]", re.UNICODE)


class EpisodeIndexError(RuntimeError):
    """当可选且可重建的 Qdrant 索引不可用时抛出的异常。"""


def tokenize(text: str) -> list[str]:
    """提取确定性的拉丁词元和单个中日韩字符词元。"""
    return [token.casefold() for token in _TOKEN_PATTERN.findall(text)]


def hashed_embedding(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """不依赖模型、密钥或网络，生成归一化的本地哈希向量。"""
    if dimension <= 0:
        raise ValueError("dimension must be positive")
    vector = [0.0] * dimension
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        position = int.from_bytes(digest[:8], "big") % dimension
        vector[position] += 1.0 if digest[8] & 1 else -1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    return vector if magnitude == 0 else [value / magnitude for value in vector]


class LocalEpisodeIndex:
    """尽力工作的本地 Qdrant 索引；调用方可在任意失败后回退。"""

    def __init__(self, path: Path, *, client: Any | None = None, models: Any | None = None) -> None:
        self.client: Any | None = client
        self._models: Any | None = models
        self._initialization_error: Exception | None = None
        try:
            if self.client is None:
                from qdrant_client import QdrantClient
                from qdrant_client.http import models as qdrant_models

                self.client = QdrantClient(path=str(path))
                self._models = qdrant_models
            active_models = self._require_models()
            if not self.client.collection_exists(COLLECTION_NAME):
                self.client.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=active_models.VectorParams(size=EMBEDDING_DIMENSION, distance=active_models.Distance.COSINE),
                )
        except Exception as error:
            self._initialization_error = error
            self.client = None

    def upsert(self, episode: Episode) -> None:
        client = self._require_client()
        try:
            models = self._require_models()

            client.upsert(
                collection_name=COLLECTION_NAME,
                points=[models.PointStruct(
                    id=episode.episode_id,
                    vector=hashed_embedding(episode.summary),
                    payload={
                        "decision_id": episode.decision_id,
                        "decision_type": episode.decision_type.value,
                        "tags": episode.tags,
                        "created_at": episode.created_at.isoformat(),
                    },
                )],
            )
        except Exception as error:
            raise EpisodeIndexError("Qdrant episode upsert failed") from error

    def search(self, query: str, decision_type: DecisionType | str, limit: int = 3) -> list[str]:
        client = self._require_client()
        wanted_type = decision_type.value if isinstance(decision_type, DecisionType) else decision_type
        try:
            models = self._require_models()

            response = client.query_points(
                collection_name=COLLECTION_NAME,
                query=hashed_embedding(query),
                query_filter=models.Filter(must=[models.FieldCondition(key="decision_type", match=models.MatchValue(value=wanted_type))]),
                limit=min(limit, 10),
            )
            points = getattr(response, "points", response)
            return [str(point.id) for point in points[: min(limit, 10)]]
        except Exception as error:
            raise EpisodeIndexError("Qdrant episode search failed") from error

    def delete(self, episode_ids: list[str]) -> None:
        """删除已确认移除的 Episode 派生向量；SQLite 仍是权威记录。"""
        if not episode_ids:
            return
        client = self._require_client()
        try:
            models = self._require_models()
            client.delete(collection_name=COLLECTION_NAME, points_selector=models.PointIdsList(points=episode_ids))
        except Exception as error:
            raise EpisodeIndexError("Qdrant episode delete failed") from error

    def _require_client(self) -> Any:
        if self.client is None:
            raise EpisodeIndexError("Qdrant episode index is unavailable") from self._initialization_error
        return self.client

    def _require_models(self) -> Any:
        if self._models is None:
            raise EpisodeIndexError("Qdrant episode models are unavailable") from self._initialization_error
        return self._models
