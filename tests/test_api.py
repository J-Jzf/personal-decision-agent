from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def client(tmp_path):
    return TestClient(create_app(Settings(
        _env_file=None, sqlite_path=tmp_path / "db.sqlite", qdrant_path=tmp_path / "qdrant",
        MCP_COMMANDS_JSON=[],
    )))


def test_decision_feedback_and_retrospective_lifecycle(tmp_path):
    with client(tmp_path) as api:
        created = api.post("/decision", json={"query": "A or B", "candidates": ["A", "B"]})
        assert created.status_code == 200
        decision_id = created.json()["decision_id"]
        assert api.post(f"/decision/{decision_id}/feedback", json={"user_choice": "A", "outcome": "满意"}).status_code == 200
        retrospective = api.post(f"/decision/{decision_id}/retrospective", json={})
        assert retrospective.status_code == 200
        assert retrospective.json()["future_lessons"]


def test_required_routes_and_unknown_id(tmp_path):
    with client(tmp_path) as api:
        assert api.get("/skills").status_code == 200
        assert api.get("/mcp/tools").status_code == 200
        assert api.get("/memory/profile").status_code == 200
        assert api.get("/memory/episodes").status_code == 200
        assert api.get("/decisions").status_code == 200
        assert api.get("/decision/missing").status_code == 404
        assert api.post("/decision/missing/continue", json={"instruction": "continue"}).status_code == 404


def test_feedback_reasons_are_extracted_into_long_term_profile_memory(tmp_path):
    """两类反馈理由都应作为用户亲自输入的画像来源，而不是仅保存原始文本。"""
    with client(tmp_path) as api:
        services = api.app.state.services

        async def extract_user_profile_signals(**kwargs):
            if kwargs["texts"] == ["A or B"]:
                return []
            assert kwargs["texts"] == ["我偏好远程工作，因为喜欢徒步。", "不选另一项因为不想搬离上海。"]
            return [
                "explicit:user_profile:hobby=hiking",
                "explicit:user_profile:home_region=shanghai",
            ]

        services.graph.judge.model_adapter.extract_user_profile_signals = extract_user_profile_signals
        created = api.post("/decision", json={"query": "A or B", "candidates": ["A", "B"]})
        decision_id = created.json()["decision_id"]
        feedback = api.post(f"/decision/{decision_id}/feedback", json={
            "user_choice": "A",
            "chosen_reason": "我偏好远程工作，因为喜欢徒步。",
            "not_chosen_reason": "不选另一项因为不想搬离上海。",
        })

        assert feedback.status_code == 200
        profiles = {(item["memory_key"], item["value"]) for item in api.get("/memory/profile").json()}
        assert ("hobby", "hiking") in profiles
        assert ("home_region", "shanghai") in profiles
