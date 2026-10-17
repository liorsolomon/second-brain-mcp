"""Vault operations over a Storage backend: notes, frontmatter, links, search."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import frontmatter

from .storage import Storage, StorageError, clean_path

WIKILINK_RE = re.compile(r"(?<!`)\[\[([^\]|#`]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

FOLDERS = {
    "inbox": "00-Inbox",
    "people": "People",
    "orgs": "Organizations",
    "meetings": "Meetings",
    "projects": "Projects",
    "areas": "Areas",
    "resources": "Resources",
    "archive": "Archive",
    "daily": "Daily",
}


class VaultError(Exception):
    pass


def safe_name(name: str) -> str:
    name = UNSAFE_NAME_RE.sub("-", name).strip().strip(".")
    if not name:
        raise VaultError("Empty note name")
    return name[:200]


def link_target(link: str) -> str:
    """'[[A/B/Name|alias]]' or 'Name' -> 'Name'."""
    m = WIKILINK_RE.search(link)
    target = m.group(1) if m else link
    return target.strip().split("/")[-1].removesuffix(".md")


def wikilink(name: str) -> str:
    return f"[[{name}]]"


@dataclass
class Note:
    path: str
    meta: dict[str, Any]
    body: str

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1].removesuffix(".md")

    @property
    def links(self) -> set[str]:
        prose = CODE_RE.sub("", self.body)  # links inside code blocks/spans aren't links
        found = {link_target(m.group(1)) for m in WIKILINK_RE.finditer(prose)}
        found |= {link_target(m.group(1)) for m in WIKILINK_RE.finditer(_flatten(self.meta))}
        return found

    def render(self) -> str:
        post = frontmatter.Post(self.body.strip() + "\n", **self.meta)
        return frontmatter.dumps(post, sort_keys=False) + "\n"

    @classmethod
    def parse(cls, path: str, text: str) -> Note:
        try:
            post = frontmatter.loads(text)
        except Exception:
            return cls(path=path, meta={}, body=text)
        meta = {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in post.metadata.items()}
        return cls(path=path, meta=meta, body=post.content)


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    return str(value)


@dataclass
class SearchHit:
    path: str
    name: str
    score: float
    type: str | None
    snippet: str


@dataclass
class Vault:
    storage: Storage
    today: Callable[[], dt.date] = dt.date.today
    _cache: dict[str, Note] = field(default_factory=dict, repr=False)

    # ---------- loading ----------

    def refresh(self) -> None:
        self._cache.clear()

    def notes(self) -> list[Note]:
        paths = self.storage.list()
        for stale in set(self._cache) - set(paths):
            del self._cache[stale]
        for p in paths:
            if p not in self._cache:
                self._cache[p] = Note.parse(p, self.storage.read(p))
        return [self._cache[p] for p in paths if not p.startswith("Templates/")]

    def resolve(self, name_or_path: str) -> Note | None:
        """Find a note by path, file name, or frontmatter alias (case-insensitive)."""
        target = name_or_path.strip()
        if target.endswith(".md") or "/" in target:
            try:
                p = clean_path(target if target.endswith(".md") else target + ".md")
            except StorageError:
                p = None
            if p and self.storage.exists(p):
                return self._load(p)
        key = link_target(target).lower()
        notes = self.notes()
        for n in notes:
            if n.name.lower() == key:
                return n
        for n in notes:
            aliases = n.meta.get("aliases") or []
            if any(str(a).lower() == key for a in aliases):
                return n
        return None

    def canonical(self, name: str) -> str:
        """Resolve a name or alias to the existing note's name; unknown names pass through."""
        note = self.resolve(link_target(name))
        return note.name if note else link_target(name)

    def require(self, name_or_path: str) -> Note:
        note = self.resolve(name_or_path)
        if note is None:
            raise VaultError(f"No note named {name_or_path!r}")
        return note

    def _load(self, path: str) -> Note:
        note = Note.parse(path, self.storage.read(path))
        self._cache[path] = note
        return note

    # ---------- writing ----------

    def save(self, note: Note) -> Note:
        note.meta["updated"] = self.today().isoformat()
        self.storage.write(note.path, note.render())
        self._cache[note.path] = note
        return note

    def create(self, folder: str, name: str, meta: dict[str, Any], body: str) -> Note:
        name = safe_name(name)
        if self.resolve(name) is not None:
            raise VaultError(f"A note named {name!r} already exists")
        today = self.today().isoformat()
        meta = {**meta}
        meta.setdefault("created", today)
        path = clean_path(f"{folder.strip('/')}/{name}.md")
        return self.save(Note(path=path, meta=meta, body=body))

    def append_to_section(self, note: Note, heading: str, line: str, top: bool = False) -> Note:
        """Add a line under '## heading'; create the section at the end if missing."""
        lines = note.body.rstrip("\n").split("\n")
        idx = next((i for i, ln in enumerate(lines) if ln.strip().lower() == f"## {heading}".lower()), None)
        if idx is None:
            note.body = note.body.rstrip("\n") + f"\n\n## {heading}\n{line}\n"
            return note
        end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        if top:
            insert_at = idx + 1
            while insert_at < end and lines[insert_at].lstrip().startswith("<!--"):
                insert_at += 1
            # drop a single empty placeholder bullet
            if insert_at < end and lines[insert_at].strip() in ("-", "- "):
                lines.pop(insert_at)
                end -= 1
        else:
            insert_at = end
            while insert_at > idx + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            if insert_at > idx + 1 and lines[insert_at - 1].strip() in ("-", "- "):
                lines.pop(insert_at - 1)
                insert_at -= 1
        lines.insert(insert_at, line)
        note.body = "\n".join(lines) + "\n"
        return note

    def archive(self, name_or_path: str) -> Note:
        note = self.require(name_or_path)
        if note.path.startswith(FOLDERS["archive"] + "/"):
            return note
        dst = clean_path(f"{FOLDERS['archive']}/{note.path}")
        self.storage.move(note.path, dst)
        self._cache.pop(note.path, None)
        note.path = dst
        note.meta["status"] = "archived"
        return self.save(note)

    def daily(self, date: dt.date | None = None) -> Note:
        d = (date or self.today()).isoformat()
        note = self.resolve(f"{FOLDERS['daily']}/{d}.md")
        if note:
            return note
        return self.create(
            FOLDERS["daily"],
            d,
            {"type": "daily", "date": d, "tags": ["daily"]},
            f"# {d}\n\n## Log\n\n## People\n\n## Captured\n",
        )

    # ---------- reading ----------

    def search(self, query: str, limit: int = 10, type: str | None = None, tag: str | None = None) -> list[SearchHit]:
        """Keyword search over names, frontmatter and body. Semantic search plugs in later."""
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
        if not terms:
            return []
        hits = []
        for n in self.notes():
            if type and n.meta.get("type") != type:
                continue
            if tag and tag.lower() not in [str(t).lower() for t in n.meta.get("tags") or []]:
                continue
            name, meta, body = n.name.lower(), _flatten(n.meta).lower(), n.body.lower()
            score = 0.0
            for t in terms:
                score += 5 * name.count(t) + 2 * meta.count(t) + min(body.count(t), 10)
            if query.lower() in name:
                score += 10
            if score:
                hits.append(SearchHit(n.path, n.name, score, n.meta.get("type"), _snippet(n.body, terms)))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    def backlinks(self, name: str) -> list[str]:
        key = link_target(name).lower()
        return [n.name for n in self.notes() if key in {lk.lower() for lk in n.links} and n.name.lower() != key]

    def health(self) -> dict[str, Any]:
        notes = self.notes()
        names = {n.name.lower() for n in notes} | {str(a).lower() for n in notes for a in (n.meta.get("aliases") or [])}
        linked = {lk.lower() for n in notes for lk in n.links}
        today = self.today()
        stale_after = {"project": 30, "project-note": 30}
        report: dict[str, Any] = {
            "notes": len(notes),
            "missing_frontmatter": [n.path for n in notes if not n.meta.get("type") and n.path != "CLAUDE.md"],
            "broken_links": sorted({f"{n.name} -> {lk}" for n in notes for lk in n.links if lk.lower() not in names}),
            "orphans": [
                n.path
                for n in notes
                if n.name.lower() not in linked and n.meta.get("type") not in ("daily", "moc") and not n.links
            ],
            "inbox": [n.path for n in notes if n.path.startswith(FOLDERS["inbox"] + "/")],
            "stale": [],
        }
        for n in notes:
            days = stale_after.get(str(n.meta.get("type")))
            updated = _as_date(n.meta.get("updated"))
            if days and updated and n.meta.get("status") == "active" and (today - updated).days > days:
                report["stale"].append(n.path)
        return report


def _as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _snippet(body: str, terms: list[str], width: int = 160) -> str:
    low = body.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, pos - width // 3)
    return " ".join(body[start : start + width].split())
