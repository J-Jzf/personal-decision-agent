"""保留证据类别并优先执行硬约束的最终决策裁判。"""

from __future__ import annotations

from models.contracts import DecisionRequest, MemoryContext


class DecisionJudge:
    def __init__(self, model_adapter) -> None:
        self.model_adapter = model_adapter

    async def decide(self, request: DecisionRequest, *, evidence_pool=None,
                     memory: MemoryContext | None = None, dimensions: list[str] | None = None,
                     execution_context: dict | None = None):
        evidence = evidence_pool.list() if evidence_pool is not None else []
        return await self.model_adapter.judge_or_fallback(
            request, evidence=evidence, memory=memory, dimensions=dimensions,
            execution_context=execution_context,
        )
