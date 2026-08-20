import asyncio

import pytest

from agents.debate import DebateArgument, DebateModerator
from evidence.pool import EvidencePool
from evidence.verifier import EvidenceVerifier
from models.contracts import AgentName, Evidence, EvidenceStatus, ToolCallStatus, ToolObservation


def item(identifier, value, source):
    return Evidence(evidence_id=identifier, decision_id="d", claim="公司融资", value=value, source=source)


def test_conflicting_claims_are_marked_and_debate_cites_ids():
    pool = EvidencePool()
    pool.add(item("EV1", "已完成", "source-a"))
    pool.add(item("EV2", "未确认", "source-b"))
    assert {entry.status for entry in pool.list()} == {EvidenceStatus.CONFLICTING}
    debate = DebateModerator(pool).run(
        [DebateArgument(side="pro", text="融资已完成", evidence_ids=["EV1"])],
        [DebateArgument(side="con", text="仍未确认", evidence_ids=["EV2"])],
    )
    assert debate.strongest_pro.evidence_ids == ["EV1"]
    with pytest.raises(ValueError):
        DebateModerator(pool).run([DebateArgument(side="pro", text="bad", evidence_ids=["missing"])], [])


def test_complementary_evidence_in_different_scopes_is_not_marked_conflicting():
    """不同城市或日期的互补资料必须共存，不能仅因文本不同而被判定冲突。"""
    pool = EvidencePool()
    pool.add(Evidence(
        evidence_id="EV1", decision_id="d", claim="周末天气",
        value="南京 8 月 22 日有雨", source="weather", scope_key="weather:nanjing:2026-08-22",
    ))
    pool.add(Evidence(
        evidence_id="EV2", decision_id="d", claim="周末天气",
        value="苏州 8 月 23 日转晴", source="weather", scope_key="weather:suzhou:2026-08-23",
    ))

    assert pool.conflicts() == []


def test_verifier_requests_second_source_for_material_single_source():
    class Gateway:
        calls = 0
        def select_tool_name(self, agent, capability, arguments):
            assert agent == AgentName.EVIDENCE_RESEARCH
            assert capability == "web_search"
            assert arguments == {"query": "price"}
            return "brave_web_search"

        async def call_tool(self, agent, tool_name, arguments, **kwargs):
            self.calls += 1
            assert tool_name == "brave_web_search"
            return ToolObservation(call_id="c", decision_id="d", task_id="verify", agent=AgentName.EVIDENCE_RESEARCH, tool_name=tool_name, status=ToolCallStatus.SUCCEEDED, result_summary="corroborated")
    pool = EvidencePool()
    pool.add(Evidence(evidence_id="EV1", decision_id="d", claim="price", value=10, source="one", confidence=.9))
    gateway = Gateway()
    verified = asyncio.run(EvidenceVerifier(gateway, pool).verify("EV1", material=True))
    assert gateway.calls == 1
    assert verified.status == EvidenceStatus.CONFIRMED
