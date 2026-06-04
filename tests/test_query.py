"""Grounding context (prime) and search."""

import datetime

from wiki_server import query


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
    write("temporal/p.md", f"---\ntype: todo\nstatus: active\ndue: {past}\n---\n\nPasse\n")
    write("temporal/f.md", f"---\ntype: event\nstatus: active\ndue: {future}\n---\n\nFutur\n")
    write("temporal/t.md", f"---\ntype: reminder\nstatus: active\ndue: {today.isoformat()}\n---\n\nAujourdhui\n")
    out = query.build_prime()
    assert "Futur" in out
    assert "Aujourdhui" in out
    assert "Passe" not in out


def test_search_returns_matches(wiki, write):
    write("long_term/people/maryse.md", "# Maryse\n\nSoeur de Florent.\n")
    out = query.search_wiki("soeur")
    assert "long_term/people/maryse.md" in out


def test_find_pages_by_name(wiki, write):
    write("long_term/people/maryse.md", "x")
    assert query.find_pages_by_name("maryse.md") == ["long_term/people/maryse.md"]
    assert query.find_pages_by_name("identity.md") == []
