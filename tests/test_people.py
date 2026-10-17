import pytest

from second_brain.people import inverse
from second_brain.vault import VaultError, link_target


def conns(note):
    return {(link_target(c["to"]), c["rel"]) for c in note.meta.get("connections") or []}


def test_inverse_pairs():
    assert inverse("reports_to") == "manages" and inverse("manages") == "reports_to"
    assert inverse("introduced") == "met_via"
    assert inverse("cofounder") == "cofounder"


def test_upsert_creates_person_and_org(people, vault):
    n, created = people.upsert(
        "Lee Chen", org="Globex", role="VP Sales", relationship="client", notes="Runs the EMEA team"
    )
    assert created and n.path == "People/Lee Chen.md"
    assert n.meta["org"] == "[[Globex]]" and n.meta["type"] == "person"
    assert "- Runs the EMEA team" in n.body
    org = vault.require("Globex")
    assert org.meta["type"] == "organization" and "[[Lee Chen]]" in org.meta["people"]


def test_upsert_matches_alias_instead_of_duplicating(people, vault):
    n, created = people.upsert("Dana", role="CEO & Chair", aliases=["DR"])
    assert not created and n.name == "Dana Rivers"
    assert n.meta["role"] == "CEO & Chair" and set(n.meta["aliases"]) == {"Dana", "DR"}
    assert len([p for p in people.people() if "Dana" in p.name]) == 1


def test_upsert_rejects_non_person_collision(people):
    with pytest.raises(VaultError):
        people.upsert("Acme Robotics")


def test_upsert_existing_org_not_duplicated(people, vault):
    people.upsert("Lee Chen", org="Acme")  # alias of Acme Robotics
    org = vault.require("Acme Robotics")
    assert "[[Lee Chen]]" in org.meta["people"]
    assert vault.resolve("Acme").path == "Organizations/Acme Robotics.md"


def test_connect_writes_both_sides_once(people, vault):
    people.upsert("Lee Chen")
    people.connect("Lee Chen", "Sam", "reports_to")
    people.connect("Lee Chen", "Sam", "reports_to")
    assert conns(vault.require("Lee Chen")) == {("Sam Okafor", "reports_to")}
    assert ("Lee Chen", "manages") in conns(vault.require("Sam Okafor"))


def test_connect_met_via_sets_field(people, vault):
    people.upsert("Lee Chen", met_via="Priya")
    lee, priya = vault.require("Lee Chen"), vault.require("Priya Nair")
    assert lee.meta["met_via"] == "[[Priya Nair]]"
    assert ("Lee Chen", "introduced") in conns(priya)


def test_connect_rejects_self_and_unknown(people):
    with pytest.raises(VaultError):
        people.connect("Dana", "Dana Rivers", "friend")
    with pytest.raises(VaultError):
        people.connect("Dana", "Nobody", "friend")


def test_log_interaction_writes_everywhere(people, vault):
    r = people.log_interaction(
        ["Dana", "Sam Okafor", "Lee Chen"],
        "Pricing review",
        title="Pricing review",
        orgs=["Acme Robotics"],
        decisions=["Go with tiered pricing"],
        follow_ups=["Send revised deck"],
        follow_up_date="2026-01-14",
    )
    assert r["meeting"] == "Meetings/2026-01-10 Pricing review.md"
    m = vault.require("2026-01-10 Pricing review")
    assert m.meta["attendees"] == ["[[Dana Rivers]]", "[[Sam Okafor]]", "[[Lee Chen]]"]
    assert "- [ ] Send revised deck" in m.body
    for name in ("Dana Rivers", "Sam Okafor", "Lee Chen"):
        p = vault.require(name)
        log = p.body.split("## Log", 1)[1]
        assert log.strip().startswith("- 2026-01-10 — Pricing review → [[2026-01-10 Pricing review]]")
        assert p.meta["last_contact"] == "2026-01-10" and p.meta["follow_up"] == "2026-01-14"
    assert "Pricing review" in vault.require("Daily/2026-01-10.md").body
    dana_log = vault.require("Dana Rivers").body.split("## Log", 1)[1]
    assert dana_log.index("2026-01-10") < dana_log.index("2026-01-05")  # newest first


def test_log_older_interaction_keeps_newer_last_contact(people, vault):
    people.log_interaction(["Dana"], "Old call", date="2025-12-01", meeting_note=False)
    assert vault.require("Dana Rivers").meta["last_contact"] == "2026-01-05"


def test_follow_ups_due(people):
    due = people.follow_ups_due(7)
    assert [d["name"] for d in due] == ["Dana Rivers"] and not due[0]["overdue"]


def test_who_knows_org_and_path(people):
    r = people.who_knows(org="Acme", target="Sam Okafor")
    assert {p["name"] for p in r["at_org"]} == {"Dana Rivers", "Sam Okafor"}
    assert [s["person"] for s in r["path_to"]][-1] == "Sam Okafor"
    assert r["path_to"][-1]["rel"] == "cofounder"


def test_graph_edges_deduplicated(people):
    g = people.graph()
    pairs = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("Dana Rivers", "Sam Okafor") in pairs and ("Sam Okafor", "Dana Rivers") not in pairs
    assert people.graph(org="Acme Robotics")["nodes"] and not people.graph(org="Globex")["nodes"]


def test_profile(people):
    p = people.profile("Dana")
    assert p["org"] == "[[Acme Robotics]]"
    assert p["meetings"] == ["2026-01-05 Robot Launch kickoff"]
    assert "Home" in p["mentioned_in"]
