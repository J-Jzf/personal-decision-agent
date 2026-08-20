import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize("query, expected_agents", [
    ("上海工作与杭州 AI Offer 怎么选", {"evidence_research", "location_lifestyle", "preference", "risk_critic"}),
    ("三款笔记本谁适合 8000 元预算", {"evidence_research", "preference", "risk_critic"}),
    ("杭州还是厦门周末旅行", {"evidence_research", "location_lifestyle", "preference", "risk_critic"}),
    ("我的 ETF 和股票是否过度集中", {"financial_market", "evidence_research", "preference", "risk_critic"}),
    ("这个 AI 课程订阅值得买吗", {"evidence_research", "preference", "risk_critic"}),
])
def test_required_decision_scenarios(tmp_path, query, expected_agents):
    settings = Settings(sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant")
    with TestClient(create_app(settings)) as api:
        response = api.post("/decision", json={"query": query})
        assert response.status_code == 200
        assert expected_agents <= set(response.json()["activated_agents"])


def test_offer_feedback_retrospective_archive(tmp_path):
    settings = Settings(sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant")
    with TestClient(create_app(settings)) as api:
        created = api.post("/decision", json={"query": "上海工作与杭州 Offer 怎么选", "candidates": ["上海", "杭州"]}).json()
        assert api.post(f"/decision/{created['decision_id']}/feedback", json={"user_choice": "杭州", "outcome": "满意"}).status_code == 200
        retrospective = api.post(f"/decision/{created['decision_id']}/retrospective", json={}).json()
        assert retrospective["decision_id"] == created["decision_id"]
