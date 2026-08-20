from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory.database import SQLiteDatabase
from memory.repositories import EpisodeRepository, ProfileRepository
from models.contracts import DecisionType, Episode


class InMemoryIndex:
    """Local test double whose observable surface matches LocalEpisodeIndex."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.items: dict[str, tuple[str, str]] = {}
        self.calls: list[tuple[str, str]] = []

    def upsert(self, episode: Episode) -> None:
        self.calls.append(("upsert", episode.episode_id))
        if self.fail:
            raise RuntimeError("index unavailable")
        self.items[episode.episode_id] = (episode.summary, episode.decision_type.value)

    def search(self, query: str, decision_type: DecisionType | str, limit: int = 3) -> list[str]:
        self.calls.append(("search", str(decision_type)))
        if self.fail:
            raise RuntimeError("index unavailable")
        wanted = decision_type.value if isinstance(decision_type, DecisionType) else decision_type
        tokens = set(query.casefold().split())
        matching = [identifier for identifier, (summary, domain) in self.items.items() if domain == wanted and tokens & set(summary.casefold().split())]
        return matching[:limit]


class RecordingQdrantClient:
    """Records the Qdrant boundary while returning the supplied similarity order."""

    def __init__(self, result_ids: list[str] | None = None) -> None:
        self.result_ids = result_ids or []
        self.events: list[tuple[str, object]] = []

    def collection_exists(self, name: str) -> bool:
        self.events.append(("collection_exists", name))
        return False

    def create_collection(self, **kwargs) -> None:
        self.events.append(("create_collection", kwargs))

    def upsert(self, **kwargs) -> None:
        self.events.append(("upsert", kwargs))

    def query_points(self, **kwargs):
        self.events.append(("query_points", kwargs))
        return type("QueryResponse", (), {"points": [type("Point", (), {"id": identifier}) for identifier in self.result_ids]})()


class FakeQdrantModels:
    class Distance:
        COSINE = "cosine"

    class VectorParams:
        def __init__(self, **kwargs) -> None: self.kwargs = kwargs

    class PointStruct:
        def __init__(self, **kwargs) -> None:
            self.id, self.vector, self.payload = kwargs["id"], kwargs["vector"], kwargs["payload"]

    class MatchValue:
        def __init__(self, **kwargs) -> None: self.value = kwargs["value"]

    class FieldCondition:
        def __init__(self, **kwargs) -> None: self.key, self.match = kwargs["key"], kwargs["match"]

    class Filter:
        def __init__(self, **kwargs) -> None: self.must = kwargs["must"]


def episode(identifier: str, domain: DecisionType, summary: str, *, tags: list[str] | None = None, signals: list[str] | None = None, age_days: int = 0) -> Episode:
    return Episode(
        episode_id=identifier, decision_id=f"decision-{identifier}", decision_type=domain,
        summary=summary, tags=tags or [], profile_signals=signals or [],
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


def build_manager(tmp_path, index: InMemoryIndex | None = None):
    from memory.manager import MemoryManager

    database = SQLiteDatabase(tmp_path / "memory.sqlite3")
    return MemoryManager(EpisodeRepository(database), ProfileRepository(database), index or InMemoryIndex())


def test_local_episode_index_sends_required_qdrant_payload_and_filter(tmp_path):
    """Changing Qdrant point metadata or its domain filter would break semantic retrieval."""
    from memory.vector_index import COLLECTION_NAME, LocalEpisodeIndex

    client = RecordingQdrantClient(result_ids=["best", "second", "third", "fourth"])
    index = LocalEpisodeIndex(tmp_path / "qdrant", client=client, models=FakeQdrantModels)
    item = episode("best", DecisionType.JOB_OFFER, "杭州 AI offer", tags=["杭州", "AI"])

    index.upsert(item)
    found = index.search("杭州 AI offer", DecisionType.JOB_OFFER, limit=9)

    upsert = next(event[1] for event in client.events if event[0] == "upsert")
    point = upsert["points"][0]
    assert upsert["collection_name"] == COLLECTION_NAME == "episodes"
    assert point.id == "best"
    assert point.payload == {"decision_id": "decision-best", "decision_type": "job_offer", "tags": ["杭州", "AI"], "created_at": item.created_at.isoformat()}
    query = next(event[1] for event in client.events if event[0] == "query_points")
    assert query["collection_name"] == "episodes"
    assert query["query_filter"].must[0].key == "decision_type"
    assert query["query_filter"].must[0].match.value == "job_offer"
    assert query["limit"] == 9
    assert found == ["best", "second", "third", "fourth"]


def test_index_search_filters_metadata_and_returns_similarity_ranking(tmp_path):
    """Removing the filter would return a travel episode for a job query."""
    manager = build_manager(tmp_path)
    manager.write_episode(episode("job", DecisionType.JOB_OFFER, "杭州 AI 岗位 offer"))
    manager.write_episode(episode("travel", DecisionType.TRAVEL, "杭州 AI 周末旅行"))

    assert [item.episode_id for item in manager.retrieve_episodes("杭州 AI offer", DecisionType.JOB_OFFER)] == ["job"]


def test_index_failure_falls_back_to_sqlite_ranking(tmp_path):
    """Removing the fallback would lose relevant history when Qdrant is down."""
    manager = build_manager(tmp_path, InMemoryIndex(fail=True))
    manager.write_episode(episode("job", DecisionType.JOB_OFFER, "杭州 AI 岗位 offer", tags=["杭州", "AI"]))
    manager.write_episode(episode("travel", DecisionType.TRAVEL, "杭州 AI 周末旅行"))

    assert [item.episode_id for item in manager.retrieve_episodes("杭州 AI offer", DecisionType.JOB_OFFER)] == ["job"]


def test_retrieve_episodes_caps_results_at_ten(tmp_path):
    """Changing the retrieval limit would exceed the context-size contract."""
    manager = build_manager(tmp_path, InMemoryIndex(fail=True))
    for number in range(11):
        manager.write_episode(episode(f"job-{number}", DecisionType.JOB_OFFER, "杭州 AI offer", age_days=number))

    assert len(manager.retrieve_episodes("杭州 AI offer", DecisionType.JOB_OFFER, limit=20)) == 10


def test_episode_is_persisted_before_index_update(tmp_path):
    """Updating the index first could advertise an episode SQLite did not save."""
    event_log: list[str] = []

    class LoggingEpisodeRepository(EpisodeRepository):
        def save(self, item):
            event_log.append("sqlite.save")
            return super().save(item)

    class LoggingIndex(InMemoryIndex):
        def upsert(self, item):
            event_log.append("index.upsert")
            return super().upsert(item)

    database = SQLiteDatabase(tmp_path / "memory.sqlite3")
    index = LoggingIndex()
    from memory.manager import MemoryManager
    manager = MemoryManager(LoggingEpisodeRepository(database), ProfileRepository(database), index)
    item = episode("job", DecisionType.JOB_OFFER, "杭州 AI 岗位")

    manager.write_episode(item)

    assert manager.episodes.get("job") == item
    assert event_log[:2] == ["sqlite.save", "index.upsert"]


def test_stale_index_ids_are_deduplicated_and_completed_from_sqlite(tmp_path):
    """Trusting stale index IDs would return too few or duplicate relevant episodes."""
    index = InMemoryIndex()
    manager = build_manager(tmp_path, index)
    first = episode("first", DecisionType.JOB_OFFER, "杭州 AI offer", age_days=0)
    second = episode("second", DecisionType.JOB_OFFER, "杭州 AI offer", age_days=1)
    third = episode("third", DecisionType.JOB_OFFER, "杭州 AI offer", age_days=2)
    wrong_type = episode("travel", DecisionType.TRAVEL, "杭州 AI offer")
    for item in [first, second, third, wrong_type]:
        manager.write_episode(item)
    index.search = lambda query, decision_type, limit: ["first", "missing", "travel", "first"]

    assert [item.episode_id for item in manager.retrieve_episodes("杭州 AI offer", DecisionType.JOB_OFFER)] == ["first", "second", "third"]


def test_explicit_preference_creates_then_changed_value_replaces_profile(tmp_path):
    """Ignoring an explicit change would retain a stale preference value."""
    manager = build_manager(tmp_path)
    first = episode("one", DecisionType.JOB_OFFER, "远程岗位", signals=["explicit:work:remote=true"])
    second = episode("two", DecisionType.JOB_OFFER, "线下岗位", signals=["explicit:work:remote=false"])

    manager.write_episode(first)
    manager.write_episode(second)

    profile = manager.profile_for("work", "remote")
    assert profile is not None
    assert profile.value is False
    assert profile.source_episode_ids == ["one", "two"]


def test_one_inferred_signal_does_not_create_profile(tmp_path):
    """Creating a profile from one inference would overfit a single decision."""
    manager = build_manager(tmp_path)

    manager.write_episode(episode("one", DecisionType.TRAVEL, "慢游", signals=["inferred:travel:pace=slow"]))

    assert manager.profile_for("travel", "pace") is None


def test_two_distinct_matching_inferences_create_profile(tmp_path):
    """Dropping corroboration would prevent stable inferred preferences."""
    manager = build_manager(tmp_path)
    manager.write_episode(episode("one", DecisionType.TRAVEL, "慢游", signals=["inferred:travel:pace=slow"]))
    manager.write_episode(episode("two", DecisionType.TRAVEL, "海边散步", signals=["inferred:travel:pace=slow"]))

    profile = manager.profile_for("travel", "pace")
    assert profile is not None
    assert profile.value == "slow"
    assert profile.source_episode_ids == ["one", "two"]


def test_conflicting_inference_reduces_confidence_without_replacing_value(tmp_path):
    """Replacing a corroborated value on conflict would turn inference into fact."""
    manager = build_manager(tmp_path)
    manager.write_episode(episode("one", DecisionType.TRAVEL, "慢游", signals=["inferred:travel:pace=slow"]))
    manager.write_episode(episode("two", DecisionType.TRAVEL, "散步", signals=["inferred:travel:pace=slow"]))
    before = manager.profile_for("travel", "pace")
    manager.write_episode(episode("three", DecisionType.TRAVEL, "赶行程", signals=["inferred:travel:pace=fast"]))

    after = manager.profile_for("travel", "pace")
    assert after is not None and before is not None
    assert after.value == "slow"
    assert after.confidence < before.confidence
    assert after.source_episode_ids == ["one", "two", "three"]


def test_context_returns_relevant_same_domain_episodes_and_profiles(tmp_path):
    """Omitting profile or domain filtering would give agents misleading context."""
    manager = build_manager(tmp_path, InMemoryIndex(fail=True))
    manager.write_episode(episode("one", DecisionType.TRAVEL, "杭州 慢游", tags=["杭州"], signals=["inferred:travel:pace=slow"]))
    manager.write_episode(episode("two", DecisionType.TRAVEL, "杭州 散步", tags=["杭州"], signals=["inferred:travel:pace=slow"]))
    manager.write_episode(episode("job", DecisionType.JOB_OFFER, "杭州 offer", tags=["杭州"]))

    context = manager.context_for("杭州 慢游", DecisionType.TRAVEL)

    assert [item.episode_id for item in context.episodes] == ["one", "two"]
    assert [(item.category, item.memory_key, item.value) for item in context.profile_memories] == [("travel", "pace", "slow")]
