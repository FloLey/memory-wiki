"""Temporal items: dated, transient things (todos, reminders, events) that live
until a date or until done, then get archived. Every item has a ``due`` date (its
"active until"); something durable with no expiry is not a temporal item, it
belongs on a long-term page. They sit in their own area ``temporal/`` next to
long_term/ and short_term/, so they do not pollute the durable knowledge.

Lifecycle: status active -> expired (the ``due`` date, meaning "active until",
has passed) or done. Nothing is ever deleted. The daemon files dated short-term
captures here and expires past ones. Files have descriptive names.
"""

from __future__ import annotations

import datetime

from wiki_server.paths import resolve_under_root, wiki_root
from wiki_server.store import fm_value, parse_frontmatter, slugify, unique_stem

TEMPORAL_DIR = "temporal"
KINDS = ("todo", "reminder", "event")


def item_stem(content: str, due: str | None, created: str | None, taken: set[str] | None = None) -> str:
    """A descriptive, unique filename stem like 2026-06-30-voyage-venise. The
    date prefix is the due date when it is a valid ISO date, else created/today,
    so a malformed due never produces a junk prefix or breaks sorting."""
    prefix = None
    if due:
        try:
            datetime.date.fromisoformat(due[:10])
            prefix = due[:10]
        except ValueError:
            pass
    if not prefix:
        prefix = (created or datetime.date.today().isoformat())[:10]
    return unique_stem(TEMPORAL_DIR, f"{prefix}-{slugify(content)}", taken)


def _render_item(kind: str, created: str | None, status: str, due: str | None, body: str) -> str:
    """Render a temporal item's file content (frontmatter + body). Shared by
    build_item (new items) and expire_changes (flipping status)."""
    kind = kind if kind in KINDS else "todo"
    fm = [f"type: {kind}", f"created: {fm_value(created or '')}", f"status: {status}"]
    if due and fm_value(due):
        fm.append(f"due: {fm_value(due)}")
    return "---\n" + "\n".join(fm) + f"\n---\n\n{body.strip()}\n"


def build_item(stem: str, kind: str, due: str | None, content: str,
               created: str | None = None, status: str = "active") -> tuple[str, str]:
    """Build (relative_path, file_content) for a temporal item. Pure, so the
    dream can batch several into one commit."""
    created = created or datetime.date.today().isoformat()
    return f"{TEMPORAL_DIR}/{stem}.md", _render_item(kind, created, status, due, content)


def list_items(active_only: bool = False) -> list[dict]:
    """All temporal items (newest first), each as a dict with path, meta, body."""
    d = resolve_under_root(TEMPORAL_DIR)
    if not d.is_dir():
        return []
    items = []
    for p in sorted(d.glob("*.md"), reverse=True):
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if active_only and meta.get("status", "active") != "active":
            continue
        items.append({"path": p.relative_to(wiki_root()).as_posix(), "meta": meta, "body": body.strip()})
    return items


def expire_changes(today: str | None = None) -> dict[str, str]:
    """Return {path: new_content} for active items whose due date (active-until)
    has passed, flipping their status to ``expired``. Pure: the caller commits."""
    today = today or datetime.date.today().isoformat()
    changes: dict[str, str] = {}
    for item in list_items():
        meta, body = item["meta"], item["body"]
        due = meta.get("due", "")
        if meta.get("status", "active") != "active" or not due:
            continue
        # Only expire on a real ISO date; a malformed due (e.g. "10 au 15 juin")
        # would compare arbitrarily and wrongly expire on the first run.
        try:
            datetime.date.fromisoformat(due)
        except ValueError:
            continue
        if due < today:
            changes[item["path"]] = _render_item(
                meta.get("type", "todo"), meta.get("created", ""), "expired", due, body)
    return changes
