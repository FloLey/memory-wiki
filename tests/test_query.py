"""Grounding context (prime), search and the folder browser."""

import datetime

import pytest

from wiki_server import query
from wiki_server.paths import WikiPathError


def test_build_prime_bundles_self_and_indexes(wiki, write):
    write("long_term/self/identity.md", "# Florent\n")
    write("long_term/index.md", "# LTM\n")
    write("short_term/index.md", "# STM\n")
    out = query.build_prime()
    assert "long_term/self/identity.md" in out
    assert "long_term/index.md" in out
    assert "short_term/index.md" in out


def test_prime_hides_past_due_temporal(wiki, write):
    today = datetime.date.today()
    past = (today - datetime.timedelta(days=2)).isoformat()
    future = (today + datetime.timedelta(days=2)).isoformat()
    write("long_term/index.md", "# LTM\n")
    # past-due event -> over, hidden
    write("temporal/passe.md", f"---\ntype: event\nstatus: active\ndue: {past}\n---\n\nSejourPasse\n")
    # past-due todo -> kept, shown as overdue
    write("temporal/retard.md", f"---\ntype: todo\nstatus: active\ndue: {past}\n---\n\nTodoEnRetard\n")
    write("temporal/f.md", f"---\ntype: event\nstatus: active\ndue: {future}\n---\n\nFutur\n")
    out = query.build_prime()
    assert "Futur" in out
    assert "SejourPasse" not in out          # past event hidden
    assert "TodoEnRetard" in out and "EN RETARD" in out  # overdue todo shown, flagged


def test_search_returns_matches(wiki, write):
    write("long_term/people/maryse.md", "# Maryse\n\nSoeur de Florent.\n")
    out = query.search_wiki("soeur")
    assert "long_term/people/maryse.md" in out


def test_find_pages_by_name(wiki, write):
    write("long_term/people/maryse.md", "x")
    assert query.find_pages_by_name("maryse.md") == ["long_term/people/maryse.md"]
    assert query.find_pages_by_name("identity.md") == []


def test_browse_root_hides_machinery(wiki, write):
    write("long_term/index.md", "x")
    write("short_term/index.md", "x")
    write("temporal/a.md", "x")
    write("DREAM.md", "x")
    write("dream_reports/r.md", "x")
    write("dream_models.json", "{}")
    subdirs, files = query.browse("")
    names = {n for n, _, _ in subdirs}
    fnames = {n for n, _ in files}
    assert {"long_term", "short_term", "temporal"} <= names
    assert "dream_reports" not in names                 # machinery folder hidden
    assert "DREAM.md" not in fnames and "dream_models.json" not in fnames


def test_browse_drills_into_folder(wiki, write):
    write("long_term/people/maryse.md", "x")
    write("long_term/index.md", "x")
    subdirs, files = query.browse("long_term")
    assert "people" in {n for n, _, _ in subdirs}
    assert ("index.md", "long_term/index.md") in files


def test_browse_counts_md(wiki, write):
    write("long_term/people/a.md", "x")
    write("long_term/people/b.md", "x")
    subdirs, _ = query.browse("long_term")
    people = next(s for s in subdirs if s[0] == "people")
    assert people[2] == 2


def test_browse_skips_private(wiki, write):
    write("long_term/private/secret.md", "x")
    write("long_term/index.md", "x")
    subdirs, _ = query.browse("long_term")
    assert "private" not in {n for n, _, _ in subdirs}


def test_browse_rejects_escape(wiki):
    with pytest.raises(WikiPathError):
        query.browse("../..")
