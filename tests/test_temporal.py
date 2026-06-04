"""Temporal items: stems, rendering, listing, expiry."""

from wiki_server import temporal


def test_item_stem_uses_due_prefix(wiki):
    assert temporal.item_stem("Voyage Venise", "2026-06-30", None).startswith("2026-06-30-")


def test_item_stem_malformed_due_falls_back(wiki):
    # A non-ISO due must not become a junk filename prefix.
    stem = temporal.item_stem("Truc", "10 au 15 juin", "2026-06-04")
    assert stem.startswith("2026-06-04-")


def test_build_item():
    rel, content = temporal.build_item("2026-06-30-x", "event", "2026-06-30", "Voyage")
    assert rel == "temporal/2026-06-30-x.md"
    assert "type: event" in content
    assert "status: active" in content
    assert "due: 2026-06-30" in content
    assert content.endswith("Voyage\n")


def test_build_item_invalid_kind_and_no_due():
    _, content = temporal.build_item("s", "bogus", None, "t")
    assert "type: todo" in content
    assert "due:" not in content


def test_list_items_active_only(wiki, write):
    write("temporal/a.md", "---\ntype: todo\nstatus: active\ndue: 2026-07-01\n---\n\nA\n")
    write("temporal/b.md", "---\ntype: todo\nstatus: expired\ndue: 2020-01-01\n---\n\nB\n")
    active = temporal.list_items(active_only=True)
    assert [i["path"] for i in active] == ["temporal/a.md"]
    assert len(temporal.list_items()) == 2


def test_expire_changes_flips_past_due(wiki, write):
    write("temporal/old.md", "---\ntype: reminder\ncreated: 2019-12-01\nstatus: active\ndue: 2020-01-01\n---\n\nRappel\n")
    changes = temporal.expire_changes("2026-06-04")
    out = changes["temporal/old.md"]
    assert "status: expired" in out
    assert "type: reminder" in out
    assert "created: 2019-12-01" in out
    assert out.endswith("Rappel\n")


def test_expire_changes_keeps_future_and_malformed(wiki, write):
    write("temporal/future.md", "---\ntype: event\nstatus: active\ndue: 2999-01-01\n---\n\nF\n")
    write("temporal/bad.md", "---\ntype: event\nstatus: active\ndue: 10 au 15 juin\n---\n\nB\n")
    assert temporal.expire_changes("2026-06-04") == {}
