"""Storage helpers for the Personal Memory Wiki.

Short-term memory (STM) is the open, fast-to-write layer. ``remember`` writes a
new entry as a markdown file, appends a row to the STM index table, and records
a git commit (prefix ``stm:``) so every capture is in the wiki's history.

Everything stays plain markdown under ``WIKI_ROOT``; there is no database.
"""

from __future__ import annotations

import re
import subprocess
import threading
import unicodedata
from datetime import datetime, timezone

from wiki_server.paths import resolve_under_root, wiki_root

STM_ENTRIES_DIR = "short_term/entries"
STM_INDEX = "short_term/index.md"

STM_INDEX_HEADER = (
    "# Short-term memory index\n\n"
    "Fresh, uncurated captures. Open and fast to write. Consolidated nightly into\n"
    "long-term memory by the dream daemon.\n\n"
    "| entry | created | summary | tags |\n"
    "|-------|---------|---------|------|\n"
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


def slugify(text: str, maxlen: int = 60) -> str:
    """A readable, ascii, hyphenated slug for a filename. Empty -> 'note'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "note"


def unique_stem(dir_rel: str, base: str, taken: set[str] | None = None) -> str:
    """A filename stem (no extension) unique in ``dir_rel`` and not in ``taken``
    (other names being created in the same batch)."""
    taken = taken or set()
    directory = resolve_under_root(dir_rel)
    candidate, n = base, 2
    while candidate in taken or (directory / f"{candidate}.md").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


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
    due: str | None = None,
    kind: str | None = None,
) -> tuple[str, str]:
    """Write a new short-term entry and update the index. Returns (name, created)
    where ``name`` is the descriptive filename stem.

    ``due`` (a date the daemon can use to file the item as temporal) and ``kind``
    (todo / reminder / event) are optional hints stored in the entry
    frontmatter; the daemon decides what to do with them."""
    clean_tags = _clean_tags(tags)
    tag_list = ", ".join(clean_tags)
    body = content.strip()
    label = summary.strip() or (body.splitlines()[0] if body.splitlines() else "note")

    with _write_lock:
        created = _utc_now_iso()
        stem = unique_stem(STM_ENTRIES_DIR, f"{created[:10]}-{slugify(label)}")

        entries_dir = resolve_under_root(STM_ENTRIES_DIR)
        entries_dir.mkdir(parents=True, exist_ok=True)

        fm = [f"created: {created}", f"tags: [{tag_list}]"]
        if due and fm_value(due):
            fm.append(f"due: {fm_value(due)}")
        if kind and fm_value(kind):
            fm.append(f"type: {fm_value(kind)}")
        resolve_under_root(f"{STM_ENTRIES_DIR}/{stem}.md").write_text(
            "---\n" + "\n".join(fm) + f"\n---\n\n{body}\n", encoding="utf-8"
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
            f.write(f"| {stem} | {created} | {row_summary} | {tag_list} |\n")

        git_commit(f"stm: remember {stem}")
        return stem, created


def fm_value(value: str) -> str:
    """Sanitize a value going into single-line frontmatter: no newlines (which
    would inject extra frontmatter keys)."""
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse simple ``key: value`` YAML-ish frontmatter. Returns (meta, body)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    meta: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            return meta, text[end + 4:].lstrip("\n")
    return meta, text


def _stm_row(path) -> str:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    created = meta.get("created", "")
    tags = meta.get("tags", "").strip().strip("[]").strip()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    summary = (lines[0] if lines else "").replace("|", "/")[:100]
    return f"| {path.stem} | {created} | {summary} | {tags} |"


def stm_index_content(exclude_stems: set[str] | None = None) -> str:
    """Rebuild the STM index content from the entry files, optionally excluding
    some entries by filename stem (used by the dream to drop consumed entries)."""
    exclude = {str(i) for i in (exclude_stems or set())}
    entries_dir = resolve_under_root(STM_ENTRIES_DIR)
    rows = []
    if entries_dir.is_dir():
        for p in sorted(entries_dir.glob("*.md")):
            if p.stem in exclude:
                continue
            rows.append(_stm_row(p))
    return STM_INDEX_HEADER + ("\n".join(rows) + "\n" if rows else "")


def apply_changes(writes: dict[str, str], deletes: list[str], message: str) -> None:
    """Apply a batch of file writes and deletes in a single commit, under the
    lock. Used by the dream execution so a whole run is one commit. Deletions are
    soft (files leave the tree but stay in git history)."""
    with _write_lock:
        for rel, content in writes.items():
            path = resolve_under_root(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for rel in deletes:
            path = resolve_under_root(rel)
            if path.is_file():
                path.unlink()
        git_commit(message)


def write_file(rel: str, content: str, message: str):
    """Write/overwrite one file under the wiki root and commit, returning its
    path. Used by the web console for manual edits (commit prefix ``manual:``)."""
    apply_changes({rel: content}, [], message)
    return resolve_under_root(rel)


def write_files(files: dict[str, str], message: str) -> None:
    """Write several files and record them in a single commit. Used by the dream
    so a run is one commit (report + usage ledger)."""
    apply_changes(files, [], message)


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
