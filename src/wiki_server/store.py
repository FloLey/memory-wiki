"""Storage helpers for the Personal Memory Wiki.

Short-term memory (STM) is the open, fast-to-write layer. ``remember`` writes a
new entry as a markdown file, appends a row to the STM index table, and records
a git commit (prefix ``stm:``) so every capture is in the wiki's history.

Everything stays plain markdown under ``WIKI_ROOT``; there is no database.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timezone

from wiki_server.paths import resolve_under_root, wiki_root

STM_ENTRIES_DIR = "short_term/entries"
STM_INDEX = "short_term/index.md"

STM_INDEX_HEADER = (
    "# Short-term memory index\n\n"
    "Fresh, uncurated captures. Open and fast to write. Consolidated nightly into\n"
    "long-term memory by the dream daemon.\n\n"
    "| id | created | summary | tags |\n"
    "|----|---------|---------|------|\n"
)

_GIT_CONFIG = [
    "-c", "user.name=wiki-server",
    "-c", "user.email=wiki-server@florent-lejoly.be",
    "-c", "commit.gpgsign=false",
]

# Serializes the whole write critical section (id allocation, file writes, index
# append, git commit). remember() is a sync tool that FastMCP may run in a
# threadpool, so concurrent calls could otherwise duplicate ids or collide on
# .git/index.lock.
_write_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    # Strip pipe/newline so a tag cannot break the markdown index table.
    cleaned = (t.strip().replace("|", "/").replace("\n", " ") for t in tags if t)
    return [t for t in cleaned if t]


def next_stm_id() -> int:
    """Next free short-term id: one past the highest numeric entry file."""
    entries = resolve_under_root(STM_ENTRIES_DIR)
    if not entries.is_dir():
        return 1
    ids = [int(p.stem) for p in entries.glob("*.md") if p.stem.isdigit()]
    return (max(ids) + 1) if ids else 1


def git_commit(message: str) -> bool:
    """Best-effort commit of the whole wiki. Never raises: a commit hiccup must
    not lose a memory that is already written to disk."""
    root = str(wiki_root())
    try:
        subprocess.run(
            ["git", "-C", root, "add", "-A"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", root, *_GIT_CONFIG, "commit", "-m", message],
            check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def write_stm_entry(
    content: str,
    summary: str = "",
    tags: list[str] | None = None,
) -> tuple[int, str]:
    """Write a new short-term entry and update the index. Returns (id, created)."""
    clean_tags = _clean_tags(tags)
    tag_list = ", ".join(clean_tags)
    body = content.strip()

    with _write_lock:
        entry_id = next_stm_id()
        created = _utc_now_iso()

        entries_dir = resolve_under_root(STM_ENTRIES_DIR)
        entries_dir.mkdir(parents=True, exist_ok=True)

        entry_path = resolve_under_root(f"{STM_ENTRIES_DIR}/{entry_id}.md")
        entry_path.write_text(
            f"---\nid: {entry_id}\ncreated: {created}\ntags: [{tag_list}]\n---\n\n{body}\n",
            encoding="utf-8",
        )

        if not summary.strip():
            lines = body.splitlines()
            summary = lines[0] if lines else ""
        # Keep the index table well-formed: collapse to one line, no pipe chars.
        row_summary = summary.replace("|", "/").replace("\n", " ").strip()[:100]

        index_path = resolve_under_root(STM_INDEX)
        if not index_path.exists():
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(STM_INDEX_HEADER, encoding="utf-8")
        with index_path.open("a", encoding="utf-8") as f:
            f.write(f"| {entry_id} | {created} | {row_summary} | {tag_list} |\n")

        git_commit(f"stm: remember entry {entry_id}")
        return entry_id, created


def write_file(rel: str, content: str, message: str):
    """Write/overwrite a file under the wiki root and commit. Used by the web
    console for manual edits (commit prefix ``manual:``). Lock-protected so it
    cannot interleave with a remember() write or another edit."""
    with _write_lock:
        path = resolve_under_root(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git_commit(message)
        return path


def delete_file(rel: str, message: str) -> bool:
    """Soft-delete a file (removed from the working tree, kept in git history)
    and commit. Returns whether the file existed."""
    with _write_lock:
        path = resolve_under_root(rel)
        if not path.is_file():
            return False
        path.unlink()
        git_commit(message)
        return True
