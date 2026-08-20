"""负责结构化证据去重、持久化同步与冲突追踪的证据池。"""

from __future__ import annotations

from models.contracts import Evidence, EvidenceStatus


class EvidencePool:
    def __init__(self, repository=None, decision_id: str | None = None) -> None:
        self.repository = repository
        self.decision_id = decision_id
        self._items: dict[str, Evidence] = {}
        if repository is not None and decision_id is not None:
            for item in repository.list():
                if item.decision_id == decision_id:
                    self._items[item.evidence_id] = item

    def add(self, item: Evidence) -> Evidence:
        """保存证据；带 scope_key 的外部观察仅与同范围资料比较，避免互补资料被误判冲突。"""
        duplicate = next((existing for existing in self._items.values()
                          if existing.claim == item.claim and existing.scope_key == item.scope_key
                          and existing.value == item.value and existing.source == item.source), None)
        if duplicate is not None:
            return duplicate
        # 结构化范围由图执行器交给模型判断关系；这里绝不能因摘要文本不同自动制造冲突。
        if item.scope_key:
            self._items[item.evidence_id] = item
            self._persist(item)
            return item
        for existing in self._items.values():
            if existing.claim.casefold() == item.claim.casefold() and existing.value != item.value:
                existing.status = EvidenceStatus.CONFLICTING
                item.status = EvidenceStatus.CONFLICTING
                existing.contradicts = list(dict.fromkeys([*existing.contradicts, item.evidence_id]))
                item.contradicts = list(dict.fromkeys([*item.contradicts, existing.evidence_id]))
                self._persist(existing)
        self._items[item.evidence_id] = item
        self._persist(item)
        return item

    def in_scope(self, scope_key: str | None) -> list[Evidence]:
        """返回同一可比较范围的已有证据，供模型判断支持、补充或矛盾。"""
        if not scope_key:
            return []
        return [item for item in self._items.values() if item.scope_key == scope_key]

    def apply_relation(self, left: Evidence, right: Evidence, relation: str) -> None:
        """仅在模型确认矛盾时标红；支持和互补关系保留为可审计关联。"""
        if relation == "contradicts":
            left.status = EvidenceStatus.CONFLICTING
            right.status = EvidenceStatus.CONFLICTING
            left.contradicts = list(dict.fromkeys([*left.contradicts, right.evidence_id]))
            right.contradicts = list(dict.fromkeys([*right.contradicts, left.evidence_id]))
        elif relation in {"supports", "complements"}:
            left.supports = list(dict.fromkeys([*left.supports, right.evidence_id]))
            right.supports = list(dict.fromkeys([*right.supports, left.evidence_id]))
        self._persist(left)
        self._persist(right)

    def save(self, item: Evidence) -> Evidence:
        """将已有证据条目的状态或支持/反对关系更新同步到持久化存储。"""
        self._items[item.evidence_id] = item
        self._persist(item)
        return item

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def list(self) -> list[Evidence]:
        return list(self._items.values())

    def conflicts(self) -> list[Evidence]:
        return [item for item in self._items.values() if item.status == EvidenceStatus.CONFLICTING]

    def _persist(self, item: Evidence) -> None:
        if self.repository is not None:
            self.repository.save(item)
