"""Path guard: the single security primitive."""

import pytest

from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root


def test_resolves_under_root(wiki):
    assert resolve_under_root("long_term/index.md") == wiki / "long_term/index.md"


def test_rejects_escape(wiki):
    with pytest.raises(WikiPathError):
        resolve_under_root("../../etc/passwd")


def test_rejects_private_area(wiki):
    with pytest.raises(WikiPathError):
        resolve_under_root("long_term/private/secret.md")


def test_wiki_root_follows_env(wiki):
    assert wiki_root() == wiki.resolve()
