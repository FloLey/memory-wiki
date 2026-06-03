"""Temporal items: dated, transient things (todos, reminders, events, temporary
memories) that live until a date or until done, then get archived. They sit in
their own area ``temporal/`` next to long_term/ and short_term/, so they do not
pollute the durable knowledge.

Lifecycle: status active -> expired (due date passed) or done. Nothing is ever
deleted. The daemon files dated short-term captures here and expires past ones.
"""

from __future__ import annotations

import datetime

from wiki_server.paths import resolve_under_root, wiki_root
from wiki_server.store import fm_value, parse_frontmatter

TEMPORAL_DIR = "temporal"
KINDS = ("todo", "reminder", "event", "souvenir")


def next_temporal_id() -> int:
    d = resolve_under_root(TEMPORAL_DIR)
    if not d.is_dir():
        return 1
    ids = [int(p.stem) for p in d.glob("*.md") if p.stem.isdigit()]
    return (max(ids) + 1) if ids else 1


def build_item(item_id: int, kind: str, due: str | None, content: str,
               created: str | None = None, status: str = "active") -> tuple[str, str]:
    """Build (relative_path, file_content) for a temporal item. Pure, so the
    dream can batch several into one commit."""
    created = fm_value(created or datetime.date.today().isoformat())
    kind = kind if kind in KINDS else "todo"
    fm = [f"id: {item_id}", f"type: {kind}", f"created: {created}", f"status: {status}"]
    if due and fm_value(due):
        fm.append(f"due: {fm_value(due)}")
    body = content.strip()
    return f"{TEMPORAL_DIR}/{item_id}.md", "---\n" + "\n".join(fm) + f"\n---\n\n{body}\n"


def list_items(active_only: bool = False) -> list[dict]:
    """All temporal items (newest id first), each as a dict with meta + body."""
    d = resolve_under_root(TEMPORAL_DIR)
    if not d.is_dir():
        return []
    items = []
    for p in sorted(d.glob("*.md"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0, reverse=True):
        try:
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if active_only and meta.get("status", "active") != "active":
            continue
        items.append({"path": p.relative_to(wiki_root()).as_posix(), "meta": meta, "body": body.strip()})
    return items


def expire_changes(today: str | None = None) -> dict[str, str]:
    """Return {path: new_content} for active items whose due date has passed,
    flipping their status to ``expired``. Pure: the caller commits."""
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
            fm = [
                f"id: {meta.get('id', '')}",
                f"type: {meta.get('type', 'todo')}",
                f"created: {meta.get('created', '')}",
                "status: expired",
                f"due: {due}",
            ]
            changes[item["path"]] = "---\n" + "\n".join(fm) + f"\n---\n\n{body}\n"
    return changes


def create_item(kind: str, due: str | None, content: str) -> str:
    """Create one temporal item directly (UI path), committing immediately."""
    from wiki_server.store import write_file

    rel, file_content = build_item(next_temporal_id(), kind, due, content)
    write_file(rel, file_content, f"manual: add temporal item {rel}")
    return rel
