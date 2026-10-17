"""Activity statistics: what occupies the user's time, from the tool calls Claude makes.

One small event per successful tool call goes to Postgres: which notes were touched, and the
tags, projects, people and organizations in their frontmatter at that moment. Never note
contents, and search text only with SB_STATS_LOG_QUERIES=true. The `activity` tool turns the
events into compact aggregates (never raw events) that Claude can chart or summarize.

Recording happens on a background thread: a tool call only puts a few strings on a queue, so a
slow or unreachable database can't slow it down or break it.
"""

from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .vault import link_target

log = logging.getLogger("second_brain.stats")

Action = Literal["read", "write", "search", "interaction"]
GroupBy = Literal["week", "month"]
Focus = Literal["topics", "notes", "people", "projects", "all"]

# Routine calls that say nothing about what the user is working on.
SKIP_TOOLS = frozenset({"context", "health", "follow_ups_due", "activity"})

WEIGHTS: dict[Action, float] = {"read": 1.0, "search": 2.0, "write": 3.0, "interaction": 4.0}
TOOL_ACTIONS: dict[str, Action] = {
    "read": "read",
    "person": "read",
    "who_knows": "read",
    "people_graph": "read",
    "search": "search",
    "capture": "write",
    "write": "write",
    "patch": "write",
    "link": "write",
    "daily": "write",
    "archive": "write",
    "upsert_person": "write",
    "connect": "write",
    "log_interaction": "interaction",
}
# Repeated reads of the same note inside this window count once (Home and CLAUDE.md get read
# at the start of every session and would otherwise dominate).
READ_DEDUPE_SECONDS = 30 * 60
SEARCH_HITS_RECORDED = 5
MAX_QUERY_CHARS = 200
# Tags that only say what kind of note something is, not what it is about.
STRUCTURAL_TAGS = frozenset({"people", "meetings", "organizations", "daily"})

NoteSnapshot = tuple[str, dict[str, Any]]  # (vault path, frontmatter)
Resolver = Callable[[list[str]], list[NoteSnapshot]]


# --------------------------------------------------------------------------- events


@dataclass
class Pending:
    """What the middleware knows right after a tool call; resolved to an Event off the request path."""

    ts: dt.datetime
    tool: str
    action: Action
    client: str
    refs: list[str]
    created_refs: list[str] = field(default_factory=list)
    query: str | None = None


@dataclass
class Event:
    ts: dt.datetime
    tool: str
    action: str
    client: str
    weight: float
    notes: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)
    query: str | None = None


def client_name(client_id: str | None) -> str:
    """A short, stable label for the MCP client behind an access token."""
    if not client_id:
        return "local"
    if "claude-code" in client_id or "claude_code" in client_id:
        return "claude-code"
    if client_id.startswith(("https://", "http://")):
        host = (urlparse(client_id).hostname or "").removeprefix("www.")
        return host or "other"
    return "other"


def _unwrap(structured: Any) -> Any:
    # FastMCP wraps non-object return values (lists) as {"result": ...}.
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def _strs(*values: Any) -> list[str]:
    return [v for v in values if isinstance(v, str) and v]


def extract_refs(tool: str, args: dict[str, Any], result: Any) -> tuple[list[str], list[str]]:
    """Note names or paths a tool call touched, and the ones it created."""
    r = _unwrap(result)
    d: dict[str, Any] = r if isinstance(r, dict) else {}
    items = [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []
    match tool:
        case "read":
            return _strs(*(x.get("path") for x in items)), []
        case "search":
            return _strs(*(x.get("path") for x in items[:SEARCH_HITS_RECORDED])), []
        case "capture" | "write":
            paths = _strs(d.get("path"))
            return paths, paths
        case "patch" | "daily" | "archive":
            return _strs(d.get("path")), []
        case "link":
            return _strs(d.get("a"), d.get("b")), []
        case "person":
            return _strs(args.get("name")), []
        case "upsert_person":
            paths = _strs(d.get("path"))
            return paths, paths if d.get("created") else []
        case "log_interaction":
            meeting = _strs(d.get("meeting"))
            people = d.get("people")
            return meeting + _strs(*(people if isinstance(people, list) else [])), meeting
        case "connect":
            return _strs(d.get("from"), d.get("to")), []
        case "who_knows":
            return _strs(args.get("org"), args.get("target")), []
        case "people_graph":
            return _strs(args.get("org")), []
    return [], []


def _values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v)]
    return []


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def note_facets(path: str, meta: dict[str, Any]) -> dict[str, list[str]]:
    """Topics, projects, people and orgs a note is about, from its path and frontmatter only."""
    name = path.rsplit("/", 1)[-1].removesuffix(".md")
    kind = meta.get("type")
    tags = [t.strip().lstrip("#").lower() for t in _values(meta.get("tags"))]
    people = [link_target(v) for k in ("attendees", "people") for v in _values(meta.get(k))]
    orgs = [link_target(v) for k in ("org", "orgs") for v in _values(meta.get(k))]
    projects = [link_target(v) for k in ("project", "projects") for v in _values(meta.get(k))]
    parts = path.split("/")
    if parts[0] == "Projects" and len(parts) > 2:
        projects.append(parts[1])
    if kind == "person":
        people.insert(0, name)
    elif kind == "organization":
        orgs.insert(0, name)
    elif kind == "project":
        projects.insert(0, name)
    return {"tags": _unique(tags), "projects": _unique(projects), "people": _unique(people), "orgs": _unique(orgs)}


def build_event(pending: Pending, snapshots: list[NoteSnapshot], created: list[str]) -> Event:
    facets: dict[str, list[str]] = {"tags": [], "projects": [], "people": [], "orgs": []}
    for path, meta in snapshots:
        for k, vs in note_facets(path, meta).items():
            facets[k].extend(vs)
    return Event(
        ts=pending.ts,
        tool=pending.tool,
        action=pending.action,
        client=pending.client,
        weight=WEIGHTS[pending.action],
        notes=_unique(p for p, _ in snapshots),
        created=_unique(created),
        query=pending.query,
        **{k: _unique(v) for k, v in facets.items()},
    )


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    tool text NOT NULL,
    action text NOT NULL,
    client text NOT NULL,
    notes text[] NOT NULL DEFAULT '{}',
    created text[] NOT NULL DEFAULT '{}',
    tags text[] NOT NULL DEFAULT '{}',
    projects text[] NOT NULL DEFAULT '{}',
    people text[] NOT NULL DEFAULT '{}',
    orgs text[] NOT NULL DEFAULT '{}',
    weight real NOT NULL,
    query text
);
CREATE INDEX IF NOT EXISTS activity_events_ts_idx ON activity_events (ts);
"""
# Serializes schema setup when several replicas start at once.
SCHEMA_LOCK_ID = 0x5B5747


class StatsStore:
    """Plain SQL over one psycopg connection, (re)opened lazily."""

    def __init__(self, url: str, timeout_s: int = 5):
        self.url = url
        self.timeout_s = timeout_s
        self._conn: Any = None

    def conn(self):
        import psycopg

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.url,
                autocommit=True,
                connect_timeout=self.timeout_s,
                options=f"-c statement_timeout={self.timeout_s * 1000}",
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def migrate(self) -> None:
        conn = self.conn()
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            conn.execute(SCHEMA)

    def insert(self, e: Event) -> None:
        self.conn().execute(
            "INSERT INTO activity_events (ts, tool, action, client, notes, created, tags, projects, people, orgs,"
            " weight, query) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                e.ts,
                e.tool,
                e.action,
                e.client,
                e.notes,
                e.created,
                e.tags,
                e.projects,
                e.people,
                e.orgs,
                e.weight,
                e.query,
            ),
        )

    def cleanup(self, retention_days: int, now: dt.datetime | None = None) -> int:
        cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=retention_days)
        cur = self.conn().execute("DELETE FROM activity_events WHERE ts < %s", (cutoff,))
        return cur.rowcount

    def events_since(self, since: dt.datetime) -> list[Event]:
        rows = self.conn().execute(
            "SELECT ts, tool, action, client, weight, notes, created, tags, projects, people, orgs"
            " FROM activity_events WHERE ts >= %s ORDER BY ts",
            (since,),
        )
        return [
            Event(
                ts=r[0],
                tool=r[1],
                action=r[2],
                client=r[3],
                weight=float(r[4]),
                notes=list(r[5]),
                created=list(r[6]),
                tags=list(r[7]),
                projects=list(r[8]),
                people=list(r[9]),
                orgs=list(r[10]),
            )
            for r in rows
        ]


# --------------------------------------------------------------------------- recording


class Recorder:
    """Resolves and writes events on one background thread. Every failure is logged and dropped."""

    RETRY_AFTER_S = 60.0
    CLEANUP_EVERY_S = 24 * 3600.0

    def __init__(
        self,
        store: StatsStore,
        resolve: Resolver,
        retention_days: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.resolve = resolve
        self.retention_days = retention_days
        self.clock = clock
        self.queue: queue.Queue[Pending] = queue.Queue(maxsize=1000)
        self._last_read: dict[str, dt.datetime] = {}
        self._ready = False
        self._down_until = 0.0
        self._last_cleanup: float | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="activity-recorder", daemon=True)
            self._thread.start()

    def submit(self, pending: Pending) -> None:
        try:
            self.queue.put_nowait(pending)
        except queue.Full:
            log.warning("activity: queue full, dropping an event")

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Wait until queued events are processed (tests and shutdown)."""
        deadline = time.monotonic() + timeout_s
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.queue.unfinished_tasks

    def _run(self) -> None:
        self._maintain()  # create the schema and prune at startup, off the startup path
        while True:
            try:
                pending = self.queue.get(timeout=60)
            except queue.Empty:
                self._maintain()
                continue
            try:
                self.process(pending)
            finally:
                self.queue.task_done()

    def _maintain(self) -> bool:
        """Connect, set up the schema and prune old events when due. False while the DB is down."""
        now = self.clock()
        if now < self._down_until:
            return False
        try:
            if not self._ready:
                self.store.migrate()
                self._ready = True
            if self._last_cleanup is None or now - self._last_cleanup >= self.CLEANUP_EVERY_S:
                self._last_cleanup = now
                deleted = self.store.cleanup(self.retention_days)
                if deleted:
                    log.info("activity: removed %d events older than %d days", deleted, self.retention_days)
            return True
        except Exception as e:
            self._fail(e)
            return False

    def _fail(self, e: Exception) -> None:
        log.warning("activity: database unavailable, not recording for %ds (%s)", self.RETRY_AFTER_S, type(e).__name__)
        self._down_until = self.clock() + self.RETRY_AFTER_S
        self._ready = False
        try:
            self.store.close()
        except Exception:  # noqa: S110 - already failing; nothing more to do
            pass

    def process(self, pending: Pending) -> Event | None:
        """Resolve, dedupe and store one event. Returns what was stored (None if skipped)."""
        try:
            event = self._build(pending)
        except Exception as e:
            log.warning("activity: could not build event for %s (%s)", pending.tool, type(e).__name__)
            return None
        if event is None or not self._maintain():
            return None
        try:
            self.store.insert(event)
            return event
        except Exception as e:
            self._fail(e)
            return None

    def _build(self, pending: Pending) -> Event | None:
        refs = _unique(pending.refs + pending.created_refs)
        snapshots = self.resolve(refs) if refs else []
        created_set = set(pending.created_refs)
        created = [p for p, _ in snapshots if p in created_set]
        if pending.action == "read" and snapshots:
            fresh = [s for s in snapshots if self._first_read(s[0], pending.ts)]
            if not fresh:
                return None
            snapshots = fresh
        return build_event(pending, snapshots, created)

    def _first_read(self, path: str, ts: dt.datetime) -> bool:
        last = self._last_read.get(path)
        if last is not None and (ts - last).total_seconds() < READ_DEDUPE_SECONDS:
            return False
        self._last_read[path] = ts
        if len(self._last_read) > 5000:
            horizon = ts - dt.timedelta(seconds=READ_DEDUPE_SECONDS)
            self._last_read = {p: t for p, t in self._last_read.items() if t >= horizon}
        return True


class StatsMiddleware(Middleware):
    """Queues one activity event per successful tool call. Never changes or fails a call."""

    def __init__(self, recorder: Recorder, log_queries: bool = False):
        self.recorder = recorder
        self.log_queries = log_queries

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        try:
            self._capture(context, result)
        except Exception as e:
            log.warning("activity: could not capture a tool call (%s)", type(e).__name__)
        return result

    def _capture(self, context: MiddlewareContext, result: Any) -> None:
        tool = context.message.name
        if tool in SKIP_TOOLS or getattr(result, "is_error", False):
            return
        args: dict[str, Any] = context.message.arguments or {}
        token = get_access_token()
        refs, created = extract_refs(tool, args, getattr(result, "structured_content", None))
        query = args.get("query") if tool == "search" and self.log_queries else None
        self.recorder.submit(
            Pending(
                ts=dt.datetime.now(dt.UTC),
                tool=tool,
                action=TOOL_ACTIONS.get(tool, "read"),
                client=client_name(token.client_id if token else None),
                refs=refs,
                created_refs=created,
                query=str(query)[:MAX_QUERY_CHARS] if query else None,
            )
        )


def build_recorder(settings: Any, resolve: Resolver) -> Recorder | None:
    """A started Recorder, or None when stats are off or no database is configured."""
    if not settings.stats:
        return None
    if settings.database_url is None:
        log.warning("Activity stats are on but no database is configured (SB_DATABASE_URL); running without them")
        return None
    recorder = Recorder(StatsStore(settings.database_url.get_secret_value()), resolve, settings.stats_retention_days)
    recorder.start()
    log.info("Activity stats on: recording to Postgres (retention %d days)", settings.stats_retention_days)
    return recorder


# --------------------------------------------------------------------------- aggregation

PERIOD_RE = re.compile(r"^\s*(\d{1,4})\s*([dwmy])\s*$")
PERIOD_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}
DIMENSIONS = {"topics": "tags", "notes": "notes", "people": "people", "projects": "projects"}
TOP_N = 10
QUIET_N = 5
QUIET_MIN_SCORE = 3.0


def parse_period(period: str) -> int:
    """'30d', '12w', '6m', '1y' -> days."""
    m = PERIOD_RE.match(period.lower())
    days = int(m.group(1)) * PERIOD_UNITS[m.group(2)] if m else 0
    if not 1 <= days <= 3650:
        raise ValueError("period must look like 30d, 12w, 6m or 1y (at most 10 years)")
    return days


def _bucket(day: dt.date, group_by: GroupBy) -> dt.date:
    return day - dt.timedelta(days=day.weekday()) if group_by == "week" else day.replace(day=1)


def _next_bucket(start: dt.date, group_by: GroupBy) -> dt.date:
    if group_by == "week":
        return start + dt.timedelta(days=7)
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _values_of(e: Event, dim: str) -> list[str]:
    values: list[str] = getattr(e, dim)
    return [v for v in values if v not in STRUCTURAL_TAGS] if dim == "tags" else values


def _scores(events: list[Event], dim: str) -> defaultdict[str, float]:
    c: defaultdict[str, float] = defaultdict(float)
    for e in events:
        for v in _values_of(e, dim):
            c[v] += e.weight
    return c


def _most(scores: dict[str, float], n: int) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _change_pct(cur: float, prev: float) -> int | None:
    return None if prev == 0 else round((cur - prev) / prev * 100)


def aggregate(
    events: list[Event],
    now: dt.datetime,
    period_days: int,
    group_by: GroupBy = "week",
    focus: Focus = "all",
) -> dict[str, Any]:
    """Compact aggregates for the current period, compared with the equal period before it."""
    start = now - dt.timedelta(days=period_days)
    prev_start = start - dt.timedelta(days=period_days)
    cur = sorted((e for e in events if start <= e.ts <= now), key=lambda e: e.ts)
    prev = [e for e in events if prev_start <= e.ts < start]
    dims = list(DIMENSIONS) if focus == "all" else [focus]

    def r(x: float) -> float:
        return round(x, 1)

    out: dict[str, Any] = {
        "period": {"days": period_days, "from": start.date().isoformat(), "to": now.date().isoformat()},
        "totals": {
            "events": len(cur),
            "score": r(sum(e.weight for e in cur)),
            "prev_events": len(prev),
            "prev_score": r(sum(e.weight for e in prev)),
            "active_days": len({e.ts.date() for e in cur}),
            "by_action": dict(Counter(e.action for e in cur).most_common()),
            "by_client": dict(Counter(e.client for e in cur).most_common()),
        },
    }

    # Time series: every bucket in range (empty ones too, so charts have no gaps).
    series: dict[dt.date, list[Event]] = {}
    b = _bucket(start.date(), group_by)
    while b <= now.date():
        series[b] = []
        b = _next_bucket(b, group_by)
    for e in cur:
        series.setdefault(_bucket(e.ts.date(), group_by), []).append(e)
    out["series"] = [
        {
            "start": b.isoformat(),
            "events": len(es),
            "score": r(sum(e.weight for e in es)),
            "top_topics": [t for t, _ in _most(_scores(es, "tags"), 3)],
        }
        for b, es in sorted(series.items())
    ]

    top: dict[str, list[dict[str, Any]]] = {}
    quiet: list[dict[str, Any]] = []
    for name in dims:
        dim = DIMENSIONS[name]
        now_s, prev_s = _scores(cur, dim), _scores(prev, dim)
        top[name] = [
            {
                "name": k,
                "score": r(v),
                "prev_score": r(prev_s.get(k, 0.0)),
                "change_pct": _change_pct(v, prev_s.get(k, 0.0)),
            }
            for k, v in _most(now_s, TOP_N)
        ]
        gone = sorted(
            ((k, v) for k, v in prev_s.items() if v >= QUIET_MIN_SCORE and k not in now_s),
            key=lambda kv: (-kv[1], kv[0]),
        )[:QUIET_N]
        for k, v in gone:
            last = max(e.ts for e in prev if k in _values_of(e, dim))
            quiet.append({"kind": name, "name": k, "prev_score": r(v), "last_seen": last.date().isoformat()})
    out["top"] = top
    out["went_quiet"] = quiet

    if focus in ("notes", "all"):
        created = _unique(p for e in reversed(cur) for p in e.created)
        out["new_notes"] = {"count": len(created), "latest": created[:TOP_N]}
    if focus in ("people", "all"):
        now_i = Counter(p for e in cur if e.tool == "log_interaction" for p in e.people)
        prev_i = Counter(p for e in prev if e.tool == "log_interaction" for p in e.people)
        out["interactions"] = [{"name": k, "count": v, "prev": prev_i[k]} for k, v in now_i.most_common(TOP_N)]
    return out
