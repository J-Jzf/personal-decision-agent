"""只允许引用证据 ID 的结构化辩论主持器。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from evidence.pool import EvidencePool


class DebateArgument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: str
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class DebateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agreements: list[str]
    disagreements: list[str]
    strongest_pro: DebateArgument | None
    strongest_con: DebateArgument | None
    unresolved_risks: list[str]
    evidence_quality: str


class DebateModerator:
    def __init__(self, pool: EvidencePool) -> None:
        self.pool = pool

    def run(self, pro: list[DebateArgument], con: list[DebateArgument]) -> DebateResult:
        known = {item.evidence_id for item in self.pool.list()}
        for argument in [*pro, *con]:
            missing = set(argument.evidence_ids) - known
            if missing:
                raise ValueError(f"debate argument cites unknown evidence IDs: {sorted(missing)}")
        conflicts = self.pool.conflicts()
        confirmed = [item for item in self.pool.list() if item.status.value == "confirmed"]
        return DebateResult(
            agreements=[item.claim for item in confirmed],
            disagreements=list(dict.fromkeys(item.claim for item in conflicts)),
            strongest_pro=self._strongest(pro), strongest_con=self._strongest(con),
            unresolved_risks=[f"证据冲突尚未解决：{item.claim}" for item in conflicts],
            evidence_quality=f"{len(confirmed)} 条已确认，{len(conflicts)} 条冲突，{len(self.pool.list())} 条总证据",
        )

    def _strongest(self, arguments: list[DebateArgument]) -> DebateArgument | None:
        confidence = {item.evidence_id: item.confidence for item in self.pool.list()}
        return max(arguments, key=lambda argument: sum(confidence.get(identifier, 0) for identifier in argument.evidence_ids), default=None)
