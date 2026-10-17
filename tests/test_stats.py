"""Activity statistics: recording middleware, aggregation and the Postgres store.

Tests marked `postgres` need a real database in SB_TEST_DATABASE_URL (CI runs a Postgres service
container) and are skipped without one. Everything else runs against an in-memory store.
"""

import datetime as dt
import logging
import os
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import second_brain.server as server_mod
import second_brain.stats as stats
from second_brain.config import Settings
from second_brain.server import build_server
from second_brain.stats import (
    Event,
    Pending,
    Recorder,
    StatsMiddleware,
    StatsStore,
    aggregate,
    client_name,
    extract_refs,
    note_facets,
    parse_period,
)

NOW = dt.datetime(2026, 3, 31, 12, tzinfo=dt.UTC)
TEST_DB = os.environ.get("SB_TEST_DATABASE_URL")


def settings(**kw) -> Settings:
    base = {"transport": "stdio", "auth": "none", "stats": True, "database_url": "postgresql://unused/db"}
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


class FakeStore:
    """In-memory stand-in for StatsStore."""

    def __init__(self, url: str = "", *_, **__):
        self.url = url
        self.events: list[Event] = []
        self.fail = False
        self.migrations = 0

    def migrate(self):
        if self.fail:
            raise ConnectionError("down")
        self.migrations += 1

    def insert(self, e: Event):
        if self.fail:
            raise ConnectionError("down")
        self.events.append(e)

    def cleanup(self, retention_days, now=None):
        return 0

    def close(self):
        pass

    def events_since(self, since):
        return [e for e in self.events if e.ts >= since]


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(stats, "StatsStore", lambda url, *a, **k: store)
    monkeypatch.setattr(server_mod, "StatsStore", lambda url, *a, **k: store)
    return store


def recorder_of(mcp) -> Recorder:
    return next(m for m in mcp.middleware if isinstance(m, StatsMiddleware)).recorder


async def call(mcp, *calls):
    async with Client(mcp) as c:
        for name, args in calls:
            await c.call_tool(name, args)
    assert recorder_of(mcp).flush()


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    "client_id,label",
    [
        (None, "local"),
        ("https://claude.ai/oauth/claude-code-client-metadata", "claude-code"),
        ("https://claude.ai/api/mcp/client-metadata", "claude.ai"),
        ("https://www.example.com/client.json", "example.com"),
        ("3f2a9c1e-random-dcr-id", "other"),
    ],
)
def test_client_name(client_id, label):
    assert client_name(client_id) == label


def test_extract_refs():
    assert extract_refs("read", {}, {"result": [{"path": "a.md"}, {"name": "x", "error": "not found"}]}) == (
        ["a.md"],
        [],
    )
    hits = {"result": [{"path": f"n{i}.md"} for i in range(8)]}
    assert extract_refs("search", {"query": "q"}, hits)[0] == [f"n{i}.md" for i in range(5)]
    assert extract_refs("write", {}, {"path": "Resources/x.md"}) == (["Resources/x.md"], ["Resources/x.md"])
    assert extract_refs("upsert_person", {}, {"path": "People/A.md", "created": False}) == (["People/A.md"], [])
    assert extract_refs("log_interaction", {}, {"meeting": "Meetings/m.md", "people": ["Dana Rivers"]}) == (
        ["Meetings/m.md", "Dana Rivers"],
        ["Meetings/m.md"],
    )
    assert extract_refs("person", {"name": "Dana"}, {"name": "Dana Rivers"}) == (["Dana"], [])
    assert extract_refs("some_future_tool", {}, None) == ([], [])


def test_note_facets(vault):
    meeting = vault.require("2026-01-05 Robot Launch kickoff")
    assert note_facets(meeting.path, meeting.meta) == {
        "tags": ["meetings"],
        "projects": ["Robot Launch"],
        "people": ["Dana Rivers"],
        "orgs": ["Acme Robotics"],
    }
    dana = vault.require("Dana Rivers")
    assert note_facets(dana.path, dana.meta)["people"] == ["Dana Rivers"]
    assert note_facets(dana.path, dana.meta)["orgs"] == ["Acme Robotics"]
    project = vault.require("Robot Launch")
    f = note_facets(project.path, project.meta)
    assert f["projects"] == ["Robot Launch"] and f["tags"] == ["acme", "launch"]


@pytest.mark.parametrize("period,days", [("30d", 30), ("12w", 84), ("6m", 180), ("1y", 365), (" 90D ", 90)])
def test_parse_period(period, days):
    assert parse_period(period) == days


@pytest.mark.parametrize("period", ["", "0d", "30", "abc", "11y", "-3d"])
def test_parse_period_rejects(period):
    with pytest.raises(ValueError):
        parse_period(period)


# --------------------------------------------------------------------------- aggregation


def ev(days_ago: float, action="read", tool=None, **kw) -> Event:
    return Event(
        ts=NOW - dt.timedelta(days=days_ago),
        tool=tool or action,
        action=action,
        client="claude.ai",
        weight=stats.WEIGHTS[action],
        **kw,
    )


@pytest.fixture
def events():
    return [
        # previous period (31-60 days ago)
        ev(40, "write", tags=["mcp"], notes=["Resources/MCP/MCP Transports.md"]),
        ev(45, "read", tags=["mcp"], notes=["Resources/MCP/MCP Transports.md"]),
        ev(50, "read", tags=["launch"], projects=["Robot Launch"]),
        ev(35, "interaction", tool="log_interaction", people=["Sam Okafor"]),
        # current period (last 30 days)
        ev(1, "write", tags=["launch", "acme"], projects=["Robot Launch"], created=["Projects/Robot Launch/Plan.md"]),
        ev(2, "read", tags=["launch"], projects=["Robot Launch"]),
        ev(3, "search", tags=["acme"]),
        ev(
            10,
            "interaction",
            tool="log_interaction",
            people=["Dana Rivers"],
            tags=["meetings"],
            created=["Meetings/2026-03-21 Pricing.md"],
        ),
        ev(11, "write", tool="upsert_person", people=["Priya Nair"]),
        ev(70, "write", tags=["old"]),  # outside both periods
    ]


def test_aggregate_totals_and_series(events):
    out = aggregate(events, NOW, 30, "week")
    t = out["totals"]
    assert t["events"] == 5
    assert t["score"] == 3 + 1 + 2 + 4 + 3
    assert t["prev_events"] == 4 and t["prev_score"] == 3 + 1 + 1 + 4
    assert t["by_action"] == {"write": 2, "read": 1, "search": 1, "interaction": 1}
    assert t["by_client"] == {"claude.ai": 5}
    assert t["active_days"] == 5
    starts = [b["start"] for b in out["series"]]
    assert starts[0] == "2026-02-23" and starts[-1] == "2026-03-30"  # Mondays covering the window
    assert all(dt.date.fromisoformat(s).weekday() == 0 for s in starts)
    assert sum(b["events"] for b in out["series"]) == 5
    last = out["series"][-1]
    assert last["top_topics"] == ["acme", "launch"]  # ties break alphabetically


def test_aggregate_monthly_buckets(events):
    out = aggregate(events, NOW, 90, "month")
    assert [b["start"] for b in out["series"]] == ["2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01"]


def test_aggregate_top_and_trend(events):
    top = aggregate(events, NOW, 30)["top"]
    launch = next(x for x in top["topics"] if x["name"] == "launch")
    assert launch == {"name": "launch", "score": 4.0, "prev_score": 1.0, "change_pct": 300}
    acme = next(x for x in top["topics"] if x["name"] == "acme")
    assert acme["change_pct"] is None  # new this period
    assert "meetings" not in {x["name"] for x in top["topics"]}  # structural tag
    assert top["projects"][0]["name"] == "Robot Launch"
    assert {x["name"] for x in top["people"]} == {"Dana Rivers", "Priya Nair"}


def test_aggregate_went_quiet(events):
    quiet = aggregate(events, NOW, 30)["went_quiet"]
    assert {"kind": "topics", "name": "mcp", "prev_score": 4.0, "last_seen": "2026-02-19"} in quiet
    assert any(q["kind"] == "people" and q["name"] == "Sam Okafor" for q in quiet)
    # below the threshold: one read of "launch" before, and it's active now anyway
    assert not any(q["name"] == "launch" for q in quiet)


def test_aggregate_new_notes_and_interactions(events):
    out = aggregate(events, NOW, 30)
    assert out["new_notes"] == {
        "count": 2,
        "latest": ["Projects/Robot Launch/Plan.md", "Meetings/2026-03-21 Pricing.md"],
    }
    assert out["interactions"] == [{"name": "Dana Rivers", "count": 1, "prev": 0}]


def test_aggregate_focus_limits_payload(events):
    out = aggregate(events, NOW, 30, focus="projects")
    assert set(out["top"]) == {"projects"}
    assert "new_notes" not in out and "interactions" not in out
    assert all(q["kind"] == "projects" for q in out["went_quiet"])


def test_aggregate_empty():
    out = aggregate([], NOW, 7)
    assert out["totals"]["events"] == 0 and out["top"]["topics"] == [] and out["went_quiet"] == []


# --------------------------------------------------------------------------- recorder


def snapshots_from(vault):
    def resolve(refs):
        notes = [vault.resolve(r) for r in refs]
        return [(n.path, n.meta) for n in notes if n]

    return resolve


def pending(minutes: float, refs, action="read", tool="read") -> Pending:
    return Pending(ts=NOW + dt.timedelta(minutes=minutes), tool=tool, action=action, client="local", refs=refs)


def test_recorder_dedupes_repeated_reads(vault):
    store = FakeStore()
    rec = Recorder(store, snapshots_from(vault), 730)  # type: ignore[arg-type]
    assert rec.process(pending(0, ["Home"]))
    assert rec.process(pending(10, ["Home"])) is None  # same note within 30 min
    e = rec.process(pending(20, ["Home", "Dana Rivers"]))
    assert e and e.notes == ["People/Dana Rivers.md"]  # only the fresh note counts
    assert rec.process(pending(31, ["Home"]))  # window passed
    # writes and searches are never deduped
    assert rec.process(pending(32, ["Home"], action="write", tool="patch"))
    assert rec.process(pending(33, ["Home"], action="write", tool="patch"))
    assert len(store.events) == 5


def test_recorder_snapshots_frontmatter_not_contents(vault):
    store = FakeStore()
    rec = Recorder(store, snapshots_from(vault), 730)  # type: ignore[arg-type]
    e = rec.process(pending(0, ["2026-01-05 Robot Launch kickoff"]))
    assert e is not None
    assert e.people == ["Dana Rivers"] and e.orgs == ["Acme Robotics"] and e.projects == ["Robot Launch"]
    body = vault.require("2026-01-05 Robot Launch kickoff").body
    assert "pilot warehouses" in body
    assert "pilot warehouses" not in repr(e)


def test_recorder_swallows_db_failures_and_backs_off(vault, caplog):
    store = FakeStore()
    store.fail = True
    clock = [0.0]
    rec = Recorder(store, snapshots_from(vault), 730, clock=lambda: clock[0])  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, "second_brain.stats"):
        assert rec.process(pending(0, ["Home"])) is None
    assert "database unavailable" in caplog.text
    store.fail = False
    clock[0] = 30  # still backing off: event dropped without touching the DB
    assert rec.process(pending(40, ["Dana Rivers"])) is None and store.migrations == 0
    clock[0] = 61
    assert rec.process(pending(80, ["Sam Okafor"])) and store.migrations == 1


def test_recorder_swallows_resolver_errors(caplog):
    def boom(refs):
        raise RuntimeError("storage down")

    rec = Recorder(FakeStore(), boom, 730)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, "second_brain.stats"):
        assert rec.process(pending(0, ["Home"])) is None
    assert "could not build event" in caplog.text


# --------------------------------------------------------------------------- middleware via the server


async def test_records_weighted_events(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    await call(
        mcp,
        ("read", {"names": ["Robot Launch"]}),
        ("search", {"query": "pricing"}),
        ("patch", {"name": "Robot Launch", "append": "- [ ] Budget", "section": "Next"}),
        ("log_interaction", {"attendees": ["Dana Rivers"], "summary": "Pricing call", "title": "Pricing call"}),
        ("connect", {"a": "Priya Nair", "b": "Sam Okafor", "rel": "friend"}),
        ("upsert_person", {"name": "Lee Chen", "org": "Globex"}),
        ("write", {"folder": "Resources/Ideas", "name": "Robot fleet", "body": "x", "tags": ["robots"]}),
    )
    by_tool = {e.tool: e for e in fake_store.events}
    assert {t: e.weight for t, e in by_tool.items()} == {
        "read": 1.0,
        "search": 2.0,
        "patch": 3.0,
        "log_interaction": 4.0,
        "connect": 3.0,
        "upsert_person": 3.0,
        "write": 3.0,
    }
    assert by_tool["log_interaction"].action == "interaction"
    assert by_tool["read"].projects == ["Robot Launch"] and by_tool["read"].client == "local"
    assert by_tool["log_interaction"].people == ["Dana Rivers"]
    assert by_tool["log_interaction"].created == ["Meetings/2026-01-10 Pricing call.md"]
    assert by_tool["upsert_person"].created == ["People/Lee Chen.md"]
    assert by_tool["write"].created == ["Resources/Ideas/Robot fleet.md"] and by_tool["write"].tags == ["robots"]
    assert by_tool["connect"].people == ["Priya Nair", "Sam Okafor"]
    assert all(e.query is None for e in fake_store.events)  # query text is opt-in


async def test_skips_routine_tools_and_failed_calls(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    await call(mcp, ("context", {"topic": "robots"}), ("health", {}), ("follow_ups_due", {}), ("activity", {}))
    async with Client(mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("person", {"name": "Nobody"})
    assert recorder_of(mcp).flush()
    assert fake_store.events == []


async def test_search_query_is_opt_in(vault, fake_store):
    mcp = build_server(settings(stats_log_queries=True), vault=vault)
    await call(mcp, ("search", {"query": "robot launch"}))
    assert fake_store.events[0].query == "robot launch"


async def test_recording_never_breaks_a_tool_call(vault, fake_store, caplog):
    fake_store.fail = True
    mcp = build_server(settings(), vault=vault)
    async with Client(mcp) as c:
        r = await c.call_tool("read", {"names": ["Home"]})
    assert r.data[0]["path"] == "Home.md"
    assert recorder_of(mcp).flush()
    assert fake_store.events == []


async def test_capture_failure_is_swallowed(vault, fake_store, monkeypatch):
    def boom(*a):
        raise RuntimeError("bad result shape")

    monkeypatch.setattr(stats, "extract_refs", boom)
    mcp = build_server(settings(), vault=vault)
    async with Client(mcp) as c:
        assert (await c.call_tool("read", {"names": ["Home"]})).data[0]["path"] == "Home.md"


async def test_activity_tool_returns_aggregates_only(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    await call(mcp, ("read", {"names": ["Robot Launch"]}), ("daily", {"entry": "worked on launch"}))
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert tools["activity"].annotations.read_only_hint is True
        out = (await c.call_tool("activity", {"period": "30d", "focus": "projects"})).data
        with pytest.raises(ToolError, match="period"):
            await c.call_tool("activity", {"period": "forever"})
    assert out["totals"]["events"] == 2
    assert out["top"]["projects"][0]["name"] == "Robot Launch"
    assert "events" not in out or not isinstance(out.get("events"), list)


async def test_stats_off_means_no_tool_and_no_recording(vault, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(stats, "StatsStore", fail)
    mcp = build_server(settings(stats=False), vault=vault)
    assert not any(isinstance(m, StatsMiddleware) for m in mcp.middleware)
    async with Client(mcp) as c:
        assert "activity" not in {t.name for t in await c.list_tools()}
        await c.call_tool("read", {"names": ["Home"]})


def test_stats_off_from_env(monkeypatch):
    monkeypatch.setenv("SB_STATS", "off")
    assert Settings(_env_file=None).stats is False  # type: ignore[call-arg]


def test_database_url_from_railway_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@postgres.railway.internal:5432/railway")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_url and s.database_url.get_secret_value().endswith("/railway")
    assert "p@" not in repr(s)  # the password never shows up in reprs or logs
    assert s.stats is False  # conftest turns stats off for tests; production default is on
    assert Settings.model_fields["stats"].default is True


async def test_missing_database_url_warns_and_runs(vault, caplog):
    with caplog.at_level(logging.WARNING, "second_brain.stats"):
        mcp = build_server(settings(database_url=None), vault=vault)
    assert "no database is configured" in caplog.text
    async with Client(mcp) as c:
        assert "activity" not in {t.name for t in await c.list_tools()}
        assert (await c.call_tool("read", {"names": ["Home"]})).data[0]["path"] == "Home.md"


async def test_unreachable_database_does_not_slow_tools(vault):
    # Nothing listens on port 1: connecting fails, on the recorder thread only.
    mcp = build_server(settings(database_url="postgresql://x:y@127.0.0.1:1/none"), vault=vault)
    async with Client(mcp) as c:
        t0 = time.monotonic()
        for _ in range(5):
            await c.call_tool("read", {"names": ["Home", "Dana Rivers"]})
        assert time.monotonic() - t0 < 2
        with pytest.raises(ToolError, match="unavailable"):
            await c.call_tool("activity", {})


# --------------------------------------------------------------------------- real Postgres

postgres = pytest.mark.skipif(not TEST_DB, reason="set SB_TEST_DATABASE_URL to run Postgres tests")


@pytest.fixture
def pg():
    assert TEST_DB
    store = StatsStore(TEST_DB)
    try:
        store.migrate()
    except Exception as e:  # pragma: no cover - local runs without a server
        if os.environ.get("CI"):
            raise  # CI provides a database: a failure there is a real failure, not a skip
        pytest.skip(f"Postgres unavailable: {type(e).__name__}")
    store.conn().execute("TRUNCATE activity_events")
    yield store
    store.close()


@postgres
def test_pg_migrate_is_idempotent(pg):
    pg.migrate()
    pg.migrate()
    cols = {
        r[0]
        for r in pg.conn().execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'activity_events'"
        )
    }
    assert {"ts", "tool", "action", "client", "notes", "tags", "projects", "people", "orgs", "weight", "query"} <= cols
    idx = [r[0] for r in pg.conn().execute("SELECT indexname FROM pg_indexes WHERE tablename = 'activity_events'")]
    assert "activity_events_ts_idx" in idx


@postgres
def test_pg_roundtrip_and_retention(pg, events):
    for e in events:
        pg.insert(e)
    got = pg.events_since(NOW - dt.timedelta(days=60))
    assert len(got) == 9 and got == sorted(got, key=lambda e: e.ts)
    assert aggregate(got, NOW, 30) == aggregate(events, NOW, 30)
    assert pg.cleanup(60, now=NOW) == 1  # the 70-day-old event
    assert len(pg.events_since(NOW - dt.timedelta(days=3650))) == 9


@postgres
async def test_pg_end_to_end(vault, pg):
    mcp = build_server(settings(database_url=TEST_DB, stats_log_queries=False), vault=vault)
    await call(
        mcp,
        ("read", {"names": ["Robot Launch"]}),
        ("read", {"names": ["Robot Launch"]}),  # deduped
        ("search", {"query": "secret plans"}),
        ("log_interaction", {"attendees": ["Dana Rivers"], "summary": "Sync"}),
    )
    rows = pg.conn().execute("SELECT tool, weight, query FROM activity_events ORDER BY id").fetchall()
    assert [(t, w) for t, w, _ in rows] == [("read", 1.0), ("search", 2.0), ("log_interaction", 4.0)]
    assert all(q is None for _, _, q in rows)
    async with Client(mcp) as c:
        out = (await c.call_tool("activity", {"period": "7d"})).data
    assert out["totals"]["events"] == 3
    assert out["interactions"] == [{"name": "Dana Rivers", "count": 1, "prev": 0}]


# --------------------------------------------------------------------------- per-topic series


def test_series_by_topic_stacks_to_score(events):
    out = aggregate(events, NOW, 30, "week")
    # acme 1.5 + 2, launch 1.5 + 1. "meetings" is structural (not a topic), so the meeting (4) and the
    # tagless upsert_person (3) count as "untagged".
    assert out["series_topics"] == ["acme", "launch"]
    for b in out["series"]:
        assert sum(b["by_topic"].values()) == pytest.approx(b["score"])
        assert set(b["by_topic"]) <= {*out["series_topics"], "other", "untagged"}
    totals = {k: sum(b["by_topic"].get(k, 0) for b in out["series"]) for k in ["acme", "launch", "untagged"]}
    assert totals == {"acme": 3.5, "launch": 2.5, "untagged": 4 + 3}


def test_series_by_topic_folds_the_rest_into_other(events, monkeypatch):
    monkeypatch.setattr(stats, "SERIES_TOPICS_N", 1)
    out = aggregate(events, NOW, 30, "week")
    assert out["series_topics"] == ["acme"]
    other = sum(b["by_topic"].get("other", 0) for b in out["series"])
    assert other == pytest.approx(2.5)  # launch
    assert all(sum(b["by_topic"].values()) == pytest.approx(b["score"]) for b in out["series"])


def test_series_by_topic_empty_bucket_is_empty(events):
    out = aggregate(events, NOW, 30, "week")
    empty = [b for b in out["series"] if b["events"] == 0]
    assert empty and all(b["by_topic"] == {} for b in empty)


# --------------------------------------------------------------------------- retrospective prompt


async def test_retrospective_prompt_registered_with_stats(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    async with Client(mcp) as c:
        assert "retrospective" in {p.name for p in await c.list_prompts()}
        text = (await c.get_prompt("retrospective", {"period": "30d"})).messages[0].content.text
    assert 'period="30d", group_by="week", focus="all"' in text
    assert "series_topics" in text and "by_topic" in text
    assert "not hours" in text


async def test_retrospective_prompt_uses_months_for_long_periods(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    async with Client(mcp) as c:
        text = (await c.get_prompt("retrospective", {"period": "1y"})).messages[0].content.text
    assert 'group_by="month"' in text and "by month" in text


async def test_retrospective_prompt_rejects_bad_period(vault, fake_store):
    mcp = build_server(settings(), vault=vault)
    async with Client(mcp) as c:
        with pytest.raises(Exception, match="period"):
            await c.get_prompt("retrospective", {"period": "forever"})


async def test_no_retrospective_prompt_without_stats(vault):
    mcp = build_server(settings(stats=False), vault=vault)
    async with Client(mcp) as c:
        assert "retrospective" not in {p.name for p in await c.list_prompts()}
