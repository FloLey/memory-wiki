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


def test_expire_keeps_overdue_todo_but_expires_event(wiki, write):
    # both 5 days past due (within the grace window)
    write("temporal/todo.md", "---\ntype: todo\nstatus: active\ndue: 2026-06-05\n---\n\nReserver voiture\n")
    write("temporal/event.md", "---\ntype: event\nstatus: active\ndue: 2026-06-05\n---\n\nSejour\n")
    changes = temporal.expire_changes("2026-06-10")
    assert "temporal/event.md" in changes          # event over -> expired
    assert "temporal/todo.md" not in changes        # overdue todo kept active


def test_expire_todo_after_grace(wiki, write):
    # a todo more than GRACE_DAYS past due finally expires
    write("temporal/old.md", "---\ntype: todo\nstatus: active\ndue: 2020-01-01\n---\n\nVieux todo\n")
    assert "temporal/old.md" in temporal.expire_changes("2026-06-10")


def test_surface_state(wiki):
    today = __import__("datetime").date(2026, 6, 10)
    # past-due todo -> shown, overdue
    assert temporal.surface_state({"type": "todo", "due": "2026-06-01"}, today) == (True, True)
    # past-due event -> hidden
    assert temporal.surface_state({"type": "event", "due": "2026-06-01"}, today) == (False, False)
    # future -> shown, not overdue
    assert temporal.surface_state({"type": "todo", "due": "2026-12-01"}, today) == (True, False)


def test_mark_done(wiki, write):
    write("temporal/t.md", "---\ntype: todo\nstatus: active\ndue: 2026-06-01\n---\n\nFaire X\n")
    assert temporal.mark_done("temporal/t.md") is True
    assert "status: done" in (wiki / "temporal/t.md").read_text()
    assert temporal.list_items(active_only=True) == []  # no longer active
    assert temporal.mark_done("temporal/missing.md") is False


def test_expire_changes_keeps_future_and_malformed(wiki, write):
    write("temporal/future.md", "---\ntype: event\nstatus: active\ndue: 2999-01-01\n---\n\nF\n")
    write("temporal/bad.md", "---\ntype: event\nstatus: active\ndue: 10 au 15 juin\n---\n\nB\n")
    assert temporal.expire_changes("2026-06-04") == {}
