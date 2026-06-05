"""One-shot migration: split a legacy ``entities`` folder into people / places /
organizations (used when the taxonomy changed). Classifies each page with one
model call, moves it, rewrites links, and regenerates the index, in one
revertible commit.
"""

from __future__ import annotations

import datetime
import json

from wiki_server.paths import resolve_under_root, wiki_root
from wiki_server.store import apply_changes, write_files

from .config import DREAM_REPORTS_DIR, USAGE_FILE, _read
from .index import _index_descriptions, _render_index
from .models import _Usage, _call_model, _model_for, _parse_json
from .runner import _guarded
from .usage import read_usage

_MIGRATION_CATEGORIES = ("people", "places", "organizations")


def _first_line(rel: str) -> str:
    """First non-empty body line of a page (frontmatter stripped), for context."""
    from wiki_server.store import parse_frontmatter

    try:
        _, body = parse_frontmatter(_read(rel))
    except Exception:
        body = _read(rel)
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    return ""


def _rewrite_links(text: str, moves: dict[str, str]) -> str:
    """Repoint markdown links from each old page path to its new one. Operates on
    the path tail (``entities/<stem>.md`` -> ``<cat>/<stem>.md``), which covers
    both ``../entities/x.md`` and ``entities/x.md`` forms; stems are unique."""
    for old_full, new_full in moves.items():
        text = text.replace(old_full[len("long_term/"):], new_full[len("long_term/"):])
    return text


def _classify_entities(usage: _Usage, items: list[tuple[str, str]]) -> dict[str, str]:
    """One model call mapping each entity stem to people / places / organizations."""
    listing = "\n".join(f"- {stem} : {first}" for stem, first in items)
    prompt = (
        "Classe chaque entité dans UNE catégorie parmi : people (une personne), "
        "places (un lieu), organizations (une organisation ou entreprise).\n"
        "Renvoie UNIQUEMENT un objet JSON, sans texte autour, de la forme "
        '{"<nom>": "people|places|organizations"}.\n\n' + listing
    )
    data = _parse_json(_call_model(usage, _model_for("triage"), prompt, 1024, "migration") or "") or {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v in _MIGRATION_CATEGORIES}


def migrate_entities() -> tuple[str, str]:
    """Move every page under long_term/entities/ into people/places/organizations,
    rewrite all links, and regenerate the index, in one revertible commit."""
    return _guarded(False, _migrate)


def _migrate(when: datetime.datetime, day: str) -> tuple[str, str]:
    rel_report = f"{DREAM_REPORTS_DIR}/{day}-migration.md"
    ent = resolve_under_root("long_term/entities")
    files = sorted(ent.glob("*.md")) if ent.is_dir() else []
    if not files:
        report = f"# Migration entities, {day}\n\nAucune page entities à migrer.\n"
        write_files({rel_report: report}, f"manual: migration report {day}")
        return rel_report, report

    usage = _Usage()
    root = wiki_root()
    items = [(p.stem, _first_line(p.relative_to(root).as_posix())) for p in files]
    mapping = _classify_entities(usage, items)

    moves: dict[str, str] = {}
    for p in files:
        cat = mapping.get(p.stem, "people")  # default to people if unsure
        moves[f"long_term/entities/{p.stem}.md"] = f"long_term/{cat}/{p.stem}.md"

    writes: dict[str, str] = {}
    deletes: list[str] = []
    final_paths: set[str] = set()
    for p in (root / "long_term").rglob("*.md"):
        rel = p.relative_to(root).as_posix()
        if rel == "long_term/index.md" or "private" in p.relative_to(root).parts:
            continue
        new_rel = moves.get(rel, rel)
        writes[new_rel] = _rewrite_links(_read(rel), moves)
        final_paths.add(new_rel)
        if new_rel != rel:
            deletes.append(rel)

    descs = {moves.get(k, k): v for k, v in _index_descriptions().items()}
    writes["long_term/index.md"] = _render_index(final_paths, descs)

    notes = [f"{old} -> {new}" for old, new in sorted(moves.items())]
    report = f"# Migration entities, {day}\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    writes[rel_report] = report
    entry = usage.entry(when)
    if entry:
        writes[USAGE_FILE] = json.dumps(read_usage() + [entry], indent=2)
    apply_changes(writes, deletes,
                  "manual: migrate entities into people/places/organizations")
    return rel_report, report
