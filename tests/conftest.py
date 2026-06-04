"""Shared test fixtures.

Tests run against a throwaway wiki under a temp dir (``WIKI_ROOT`` is read
dynamically by ``paths.wiki_root``). Git commits are stubbed so the suite is fast
and independent of the environment; the tests assert on the working-tree content,
which is what the code guarantees, not on git mechanics.
"""

from __future__ import annotations

import subprocess

import pytest

from wiki_server import store


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """A fresh, empty wiki rooted at a temp dir, with git commits stubbed."""
    root = tmp_path / "wiki"
    for d in ("long_term/self", "short_term/entries", "temporal", "dream_reports"):
        (root / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_ROOT", str(root))
    subprocess.run(["git", "init", "-q", str(root)], check=False)
    monkeypatch.setattr(store, "git_commit", lambda message: True)
    return root


@pytest.fixture
def write(wiki):
    """Helper to drop a file into the wiki, creating parent dirs."""

    def _write(rel: str, content: str):
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    return _write
