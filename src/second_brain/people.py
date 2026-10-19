"""People, organizations, meetings, and the connections between people."""

from __future__ import annotations

import datetime as dt
from collections import deque
from typing import Any

from .vault import FOLDERS, Note, Vault, VaultError, _as_date, link_target, safe_name, wikilink

INVERSE = {
    "reports_to": "manages",
    "introduced": "met_via",
    "investor_in": "investor",
    "advisor_to": "advisee",
    "client_of": "vendor_to",
    "parent": "child",
}
INVERSE |= {v: k for k, v in INVERSE.items()}
# symmetric: cofounder, colleague, partner, spouse, sibling, friend, and anything unknown

PERSON_BODY = """# {name}

## Who they are
{who}

## What we do together
-

## Personal
-

## Log
"""

PERSON_FIELDS = (
    "aliases",
    "org",
    "role",
    "relationship",
    "context",
    "met",
    "met_via",
    "last_contact",
    "follow_up",
    "email",
    "phone",
)

# Contact PII. The single-person `person` lookup returns these, but the bulk `read` tool
# strips them from frontmatter so one call can't harvest contact details for many people at
# once (e.g. a prompt-injected session reading every note in the People folder).
CONTACT_FIELDS = ("email", "phone")


def inverse(rel: str) -> str:
    return INVERSE.get(rel, rel)


def _is(note: Note, type_: str) -> bool:
    return note.meta.get("type") == type_


class People:
    def __init__(self, vault: Vault):
        self.v = vault

    # ---------- lookup ----------

    def people(self) -> list[Note]:
        return [n for n in self.v.notes() if _is(n, "person")]

    def get(self, name: str) -> Note:
        note = self.v.resolve(name)
        if note is None or not _is(note, "person"):
            raise VaultError(f"No person named {name!r}")
        return note

    def profile(self, name: str) -> dict[str, Any]:
        p = self.get(name)
        log = _section(p.body, "Log")
        meetings = [
            m.name
            for m in self.v.notes()
            if _is(m, "meeting") and p.name.lower() in {link_target(a).lower() for a in m.meta.get("attendees") or []}
        ]
        return {
            "name": p.name,
            "path": p.path,
            **{k: p.meta.get(k) for k in PERSON_FIELDS if p.meta.get(k) not in (None, "", [])},
            "connections": p.meta.get("connections") or [],
            "recent_log": [ln for ln in log if ln.startswith("- ")][:10],
            "meetings": sorted(meetings, reverse=True)[:10],
            "mentioned_in": self.v.backlinks(p.name)[:25],
            "body": p.body,
        }

    # ---------- write ----------

    def upsert(self, name: str, **fields: Any) -> tuple[Note, bool]:
        """Create or update a person. Returns (note, created)."""
        fields = {k: v for k, v in fields.items() if v not in (None, "", [])}
        unknown = set(fields) - set(PERSON_FIELDS) - {"notes"}
        if unknown:
            raise VaultError(f"Unknown person fields: {sorted(unknown)}")
        notes_text = fields.pop("notes", None)
        if "org" in fields:
            fields["org"] = wikilink(self.v.canonical(fields["org"]))
        if "met_via" in fields:
            fields["met_via"] = wikilink(self.v.canonical(fields["met_via"]))

        existing = self.v.resolve(name)
        if existing is not None and not _is(existing, "person"):
            raise VaultError(f"{name!r} exists but is a {existing.meta.get('type')} note, not a person")
        created = existing is None
        if created:
            meta = {"type": "person", "aliases": [], "connections": [], "tags": ["people"]}
            meta.update(fields)
            who = f"- {notes_text}" if notes_text else "-"
            note = self.v.create(FOLDERS["people"], name, meta, PERSON_BODY.format(name=safe_name(name), who=who))
        else:
            note = existing
            for k, v in fields.items():
                if k == "aliases":
                    note.meta["aliases"] = sorted({*note.meta.get("aliases", []), *v})
                else:
                    note.meta[k] = v
            if notes_text:
                self.v.append_to_section(note, "Who they are", f"- {notes_text}")
            self.v.save(note)

        if "org" in fields:
            self._add_to_org(fields["org"], note.name)
        if "met_via" in fields:
            self.connect(note.name, link_target(fields["met_via"]), "met_via")
        return note, created

    def _add_to_org(self, org: str, person: str) -> None:
        org_name = link_target(org)
        org_note = self.v.resolve(org_name)
        if org_note is None:
            org_note = self.v.create(
                FOLDERS["orgs"],
                org_name,
                {"type": "organization", "aliases": [], "people": [], "tags": ["organizations"]},
                f"# {safe_name(org_name)}\n\n## What they are\n-\n\n## How I'm involved\n-\n\n## Log\n",
            )
        people = org_note.meta.get("people") or []
        if person.lower() not in {link_target(p).lower() for p in people}:
            org_note.meta["people"] = [*people, wikilink(person)]
            self.v.save(org_note)

    def connect(self, a: str, b: str, rel: str) -> dict[str, str]:
        """Record a connection on both people, with the inverse relation on the other side."""
        pa, pb = self.get(a), self.get(b)
        if pa.path == pb.path:
            raise VaultError("Cannot connect a person to themselves")
        rel = rel.strip().lower().replace(" ", "_")
        for src, dst, r in ((pa, pb, rel), (pb, pa, inverse(rel))):
            conns = list(src.meta.get("connections") or [])
            if not any(link_target(c.get("to", "")).lower() == dst.name.lower() and c.get("rel") == r for c in conns):
                conns.append({"to": wikilink(dst.name), "rel": r})
                src.meta["connections"] = conns
            if r == "met_via" and not src.meta.get("met_via"):
                src.meta["met_via"] = wikilink(dst.name)
            self.v.save(src)
        return {"from": pa.name, "to": pb.name, "rel": rel, "inverse": inverse(rel)}

    def log_interaction(
        self,
        attendees: list[str],
        summary: str,
        title: str | None = None,
        date: str | None = None,
        channel: str = "",
        orgs: list[str] | None = None,
        projects: list[str] | None = None,
        decisions: list[str] | None = None,
        follow_ups: list[str] | None = None,
        follow_up_date: str | None = None,
        meeting_note: bool = True,
    ) -> dict[str, Any]:
        """Log a conversation: a Meeting note plus a line in each attendee's Log."""
        if not attendees:
            raise VaultError("At least one attendee is required")
        day = (dt.date.fromisoformat(date) if date else self.v.today()).isoformat()
        people = []
        for name in attendees:
            note = self.v.resolve(name)
            if note is None:
                note, _ = self.upsert(name)
            elif not _is(note, "person"):
                raise VaultError(f"{name!r} is not a person note")
            people.append(note)

        meeting = None
        if meeting_note:
            mtitle = f"{day} {title or ', '.join(p.name for p in people)}"
            body = f"# {mtitle}\n\n## Summary\n- {summary}\n\n## Decisions\n"
            body += "".join(f"- {d}\n" for d in decisions or []) or "-\n"
            body += "\n## Follow-ups\n" + ("".join(f"- [ ] {f}\n" for f in follow_ups or []) or "- [ ]\n")
            meeting = self.v.create(
                FOLDERS["meetings"],
                mtitle,
                {
                    "type": "meeting",
                    "date": day,
                    "attendees": [wikilink(p.name) for p in people],
                    "orgs": [wikilink(self.v.canonical(o)) for o in orgs or []],
                    "projects": [wikilink(self.v.canonical(p)) for p in projects or []],
                    "channel": channel,
                    "tags": ["meetings"],
                },
                body,
            )

        ref = f" → {wikilink(meeting.name)}" if meeting else ""
        for p in people:
            self.v.append_to_section(p, "Log", f"- {day} — {summary}{ref}", top=True)
            prev = _as_date(p.meta.get("last_contact"))
            if prev is None or prev.isoformat() <= day:
                p.meta["last_contact"] = day
            if follow_up_date:
                p.meta["follow_up"] = follow_up_date
            self.v.save(p)

        daily = self.v.daily(dt.date.fromisoformat(day))
        self.v.append_to_section(daily, "People", f"- {', '.join(wikilink(p.name) for p in people)}: {summary}{ref}")
        self.v.save(daily)
        return {"meeting": meeting.path if meeting else None, "people": [p.name for p in people], "date": day}

    # ---------- queries ----------

    def follow_ups_due(self, within_days: int = 7) -> list[dict[str, Any]]:
        horizon = self.v.today() + dt.timedelta(days=within_days)
        out = []
        for p in self.people():
            due = _as_date(p.meta.get("follow_up"))
            if due and due <= horizon:
                out.append(
                    {
                        "name": p.name,
                        "follow_up": due.isoformat(),
                        "overdue": due < self.v.today(),
                        "last_contact": str(p.meta.get("last_contact") or ""),
                    }
                )
        return sorted(out, key=lambda r: r["follow_up"])

    def _org_of(self, p: Note) -> str:
        return link_target(str(p.meta.get("org") or "")).lower()

    def graph(self, org: str | None = None, context: str | None = None) -> dict[str, Any]:
        nodes, edges = [], []
        keep = set()
        org_key = self.v.canonical(org).lower() if org else None
        for p in self.people():
            if org_key and self._org_of(p) != org_key:
                continue
            if context and context.lower() not in [str(c).lower() for c in p.meta.get("context") or []]:
                continue
            keep.add(p.name.lower())
            nodes.append(
                {
                    "name": p.name,
                    "org": link_target(str(p.meta.get("org") or "")) or None,
                    "relationship": p.meta.get("relationship"),
                }
            )
        for p in self.people():
            if p.name.lower() not in keep:
                continue
            for c in p.meta.get("connections") or []:
                to = link_target(c.get("to", ""))
                if to.lower() in keep and p.name.lower() < to.lower():
                    edges.append({"from": p.name, "to": to, "rel": c.get("rel")})
        return {"nodes": nodes, "edges": edges}

    def who_knows(self, org: str | None = None, target: str | None = None) -> dict[str, Any]:
        """People at an org, and/or the shortest connection path to a person."""
        result: dict[str, Any] = {}
        if org:
            key = self.v.canonical(org).lower()
            result["at_org"] = [
                {"name": p.name, "role": p.meta.get("role"), "relationship": p.meta.get("relationship")}
                for p in self.people()
                if self._org_of(p) == key
            ]
        if target:
            result["path_to"] = self._path_to(target)
        return result

    def _path_to(self, target: str) -> list[dict[str, str]] | None:
        """BFS from people I have a direct relationship with (non-empty `relationship`)."""
        goal = self.get(target).name
        adj: dict[str, list[tuple[str, str]]] = {}
        for p in self.people():
            for c in p.meta.get("connections") or []:
                adj.setdefault(p.name, []).append((link_target(c.get("to", "")), c.get("rel", "")))
        starts = [p.name for p in self.people() if p.meta.get("relationship")]
        if goal in starts:
            return [{"person": goal, "rel": "direct"}]
        prev: dict[str, tuple[str, str]] = {}
        seen, queue = set(starts), deque(starts)
        while queue:
            cur = queue.popleft()
            for nxt, rel in adj.get(cur, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (cur, rel)
                if nxt == goal:
                    path = [{"person": goal, "rel": rel}]
                    node = cur
                    while node in prev:
                        p, r = prev[node]
                        path.append({"person": node, "rel": r})
                        node = p
                    path.append({"person": node, "rel": "direct"})
                    return list(reversed(path))
                queue.append(nxt)
        return None


def _section(body: str, heading: str) -> list[str]:
    lines = body.split("\n")
    try:
        i = next(i for i, ln in enumerate(lines) if ln.strip().lower() == f"## {heading}".lower())
    except StopIteration:
        return []
    out = []
    for ln in lines[i + 1 :]:
        if ln.startswith("## "):
            break
        out.append(ln)
    return out
