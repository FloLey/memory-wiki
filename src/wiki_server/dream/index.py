"""Long-term index regeneration.

The index is rebuilt by code (never by the model) from the page descriptions and
the set of pages on disk, so it always reflects reality.
"""

from __future__ import annotations

import re

from wiki_server.paths import wiki_root

from .config import _CATEGORIES, _read


def _index_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read("long_term/index.md").splitlines():
        m = re.match(r"\s*-\s*\[[^\]]*\]\(([^)]+)\)\s*:?\s*(.*)", line)
        if m:
            rel = m.group(1)
            full = rel if rel.startswith("long_term/") else f"long_term/{rel}"
            out[full] = m.group(2).strip()
    return out


def _render_index(paths: set[str], descs: dict[str, str]) -> str:
    """Render the index from an explicit page set, grouped by top-level folder:
    the known categories first (always shown, even empty), then any other folder
    that still has pages (e.g. a legacy ``entities`` not yet migrated), so nothing
    silently drops out of the index."""
    by_folder: dict[str, list[str]] = {}
    for full in paths:
        if not full.startswith("long_term/") or full == "long_term/index.md":
            continue
        parts = full.split("/")
        if len(parts) < 3:
            continue
        by_folder.setdefault(parts[1], []).append(full)
    extra = sorted(f for f in by_folder if f not in _CATEGORIES)
    lines = ["# Long-term memory index", "", "Catalogue des pages durables, par catégorie.", ""]
    for folder in list(_CATEGORIES) + extra:
        lines.append(f"## {folder}")
        for full in sorted(by_folder.get(folder, [])):
            rel_to_ltm = full[len("long_term/"):]
            stem = full.rsplit("/", 1)[-1][:-3]
            desc = descs.get(full, "")
            lines.append(f"- [{stem}]({rel_to_ltm})" + (f" : {desc}" if desc else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _regenerate_index(new_descriptions: dict[str, str], extra_pages: set[str]) -> str:
    descs = _index_descriptions()
    descs.update({k: v for k, v in new_descriptions.items() if v})
    root, ltm = wiki_root(), wiki_root() / "long_term"
    paths = set(extra_pages)
    if ltm.is_dir():
        for p in ltm.rglob("*.md"):
            rel = p.relative_to(root).as_posix()
            if rel == "long_term/index.md" or "private" in p.relative_to(root).parts:
                continue
            paths.add(rel)
    return _render_index(paths, descs)
