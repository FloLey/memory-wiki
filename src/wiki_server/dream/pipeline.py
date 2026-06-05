"""The three-stage pipeline and the dry-run / execute entry points.

triage clusters and routes short-term memory; decide chooses, per unit, what to
do with the pages it touches; write produces each page's final content. Execute
applies everything in one revertible commit (per-element, cumulative, atomic per
unit); the dry-run stops after decide and reports the plan.
"""

from __future__ import annotations

import datetime
import json

from wiki_server import temporal
from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import apply_changes, stm_index_content, write_files

from .config import DREAM_REPORTS_DIR, USAGE_FILE, _read, ensure_policy
from .index import _regenerate_index
from .models import _Usage, _stage
from .runner import _guarded
from .usage import read_usage


def _read_stm_entries() -> list[tuple[str, str]]:
    entries_dir = resolve_under_root("short_term/entries")
    if not entries_dir.is_dir():
        return []
    out = []
    for p in sorted(entries_dir.glob("*.md")):
        try:
            out.append((p.name, p.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _stm_stem(name: str) -> str:
    name = str(name).strip().split("/")[-1]
    return name[:-3] if name.endswith(".md") else name


def _as_list(value) -> list:
    """Normalize a field that should be a list but the model may return as a
    single object or null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _decisions(usage: _Usage, policy: str, stm_entries: list[tuple[str, str]], notes: list) -> list[dict]:
    """Stage 1 (triage) + stage 2 (decide per unit). Returns [{unit, decision}]."""
    stm_map = {name: body for name, body in stm_entries}
    stm_block = "\n\n".join(f"### {name}\n{body}" for name, body in stm_entries)
    ltm_index = _read("long_term/index.md") or "(vide)"
    triage = _stage(usage, "triage",
                    f"<policy>\n{policy}\n</policy>\n\n"
                    f"<long_term_index>\n{ltm_index}\n</long_term_index>\n\n"
                    f"<short_term>\n{stm_block}\n</short_term>")
    if not triage or not isinstance(triage.get("units"), list):
        notes.append("Triage : aucune unité exploitable.")
        return []

    pairs = []
    for unit in triage["units"]:
        if not isinstance(unit, dict):
            continue
        names = [n for n in (unit.get("stm") or []) if isinstance(n, str)]
        unit_stm = "\n\n".join(
            f"### {n}\n{stm_map.get(n) or stm_map.get(n + '.md') or ''}" for n in names
        )
        touches = [t for t in (unit.get("touches") or []) if isinstance(t, str) and t.startswith("long_term/")]
        pages_block = "\n\n".join(
            f"### {t}\n{_read(t) or '(nouvelle page, vide)'}" for t in touches
        ) or "(aucune page touchée)"
        decision = _stage(usage, "decide",
                          f"<policy>\n{policy}\n</policy>\n\n"
                          f"<unit intent=\"{unit.get('intent', '')}\">\n{unit_stm}\n</unit>\n\n"
                          f"<touched_pages>\n{pages_block}\n</touched_pages>")
        if decision:
            pairs.append({"unit": unit, "decision": decision})
        else:
            notes.append(f"Décision illisible pour : {unit.get('intent', names)}")
    return pairs


def _write_page(usage: _Usage, policy: str, op: dict, current: str = "") -> dict | None:
    """Stage 3: produce {content, description} for one integrate/promote page op.

    ``current`` is the page's content as it stands *at this point in the dream*:
    the caller passes what an earlier unit already produced for this path, so a
    page touched by several units is built cumulatively instead of overwritten."""
    page = op.get("page", "")
    out = _stage(usage, "write",
                 f"<policy>\n{policy}\n</policy>\n\n"
                 f"<operation>\n{json.dumps(op, ensure_ascii=False)}\n</operation>\n\n"
                 f"<current_page path=\"{page}\">\n{current or '(nouvelle page)'}\n</current_page>")
    return out if out and isinstance(out.get("content"), str) else None


def _format_decisions(pairs: list[dict]) -> str:
    if not pairs:
        return "Aucune décision proposée."
    blocks = []
    for pair in pairs:
        u, d = pair["unit"], pair["decision"]
        stm_names = ", ".join(s for s in (u.get("stm") or []) if isinstance(s, str))
        lines = [f"## {u.get('intent', '(unité)')}", f"- entrées : {stm_names}"]
        pages_ops = _as_list(d.get("pages"))
        temporal_ops = _as_list(d.get("temporal"))
        for op in pages_ops:
            if isinstance(op, dict) and op.get("page"):
                lines.append(f"- {op.get('action', 'write')} {op['page']} : {op.get('change', '')}")
        for t in temporal_ops:
            if isinstance(t, dict) and t.get("content"):
                lines.append(f"- temporal : {t.get('type')} (due {t.get('due')}) : {t.get('content')}")
        if not pages_ops and not temporal_ops:
            lines.append("- action : garder en court terme")
        if d.get("rationale"):
            lines.append(f"- pourquoi : {d['rationale']}")
        blocks.append("\n".join(lines))

    # Pages touched by more than one unit are built cumulatively at execute time
    # (one merged page), not duplicated. Flag them so the dry-run reflects reality.
    counts: dict[str, int] = {}
    for pair in pairs:
        for op in _as_list(pair["decision"].get("pages")):
            if isinstance(op, dict) and isinstance(op.get("page"), str):
                counts[op["page"]] = counts.get(op["page"], 0) + 1
    merged = sorted(p for p, c in counts.items() if c > 1)
    body = "\n\n".join(blocks)
    if merged:
        body += "\n\n---\n\n" + "\n".join(
            f"- {p} : touchée par {counts[p]} unités, fusionnée en une seule page."
            for p in merged
        )
    return body


def run_dry_run() -> tuple[str, str]:
    """Triage + decide, reported for review. Changes no memory."""
    return _guarded(True, _dry_run)


def _dry_run(when: datetime.datetime, day: str) -> tuple[str, str]:
    policy = ensure_policy()
    stm_entries = _read_stm_entries()
    usage = _Usage()
    notes: list[str] = []
    if not stm_entries:
        body = "Mémoire court terme vide. Rien à consolider."
    else:
        body = _format_decisions(_decisions(usage, policy, stm_entries, notes))
    if notes:
        body += "\n\n---\n\n" + "\n".join(f"- {n}" for n in notes)
    cost_lines = usage.cost_lines()
    if cost_lines:
        body += "\n\n## Coût par étape\n\n" + "\n".join(f"- {c}" for c in cost_lines)

    report = f"# Dream dry-run, {day}\n\n{body}\n"
    rel = f"{DREAM_REPORTS_DIR}/{day}-dryrun.md"
    files = {rel: report}
    entry = usage.entry(when)
    if entry:
        files[USAGE_FILE] = json.dumps(read_usage() + [entry], indent=2)
    write_files(files, f"dream: dry-run report {day}")
    return rel, report


def run_execute() -> tuple[str, str]:
    """Triage + decide + write, applied in one revertible commit."""
    return _guarded(False, _execute)


def _execute(when: datetime.datetime, day: str) -> tuple[str, str]:
    policy = ensure_policy()
    stm_entries = _read_stm_entries()
    usage = _Usage()
    notes: list[str] = []
    writes: dict[str, str] = {}
    deletes: list[str] = []

    pairs = _decisions(usage, policy, stm_entries, notes) if stm_entries else []
    if not stm_entries:
        notes.append("Mémoire court terme vide.")

    new_desc: dict[str, str] = {}
    page_paths: set[str] = set()
    consumed: set[str] = set()
    temporal_taken: set[str] = set()

    for pair in pairs:
        u, d = pair["unit"], pair["decision"]
        stm_stems = {_stm_stem(n) for n in (u.get("stm") or []) if isinstance(n, str)}
        # Per-element: every output that succeeds is applied. The short-term
        # entries are only kept (for the next dream) when something in the unit
        # failed, so what landed is durable and only the missing part is retried.
        u_writes: dict[str, str] = {}
        u_desc: dict[str, str] = {}
        u_pages: set[str] = set()
        u_temporal: set[str] = set()
        u_notes: list[str] = []
        failed = False

        for op in _as_list(d.get("pages")):
            if not isinstance(op, dict):
                continue
            page = op.get("page")
            if not (isinstance(page, str) and page.startswith("long_term/") and page.endswith(".md")):
                u_notes.append(f"Page invalide : {page!r}")
                failed = True
                continue
            try:
                resolve_under_root(page)
            except WikiPathError:
                u_notes.append(f"Page interdite : {page!r}")
                failed = True
                continue
            # Cumulative build: start from what this dream already produced for
            # this path (an earlier unit, then this unit), else the on-disk page.
            # So several units that touch the same page merge instead of clobber.
            current = u_writes.get(page) or writes.get(page) or _read(page)
            written = _write_page(usage, policy, op, current)
            if not written:
                u_notes.append(f"Écriture échouée pour {page}.")
                failed = True
                continue
            u_writes[page] = written["content"]
            u_pages.add(page)
            if isinstance(written.get("description"), str) and written["description"].strip():
                u_desc[page] = written["description"].strip()

        for t in _as_list(d.get("temporal")):
            if not isinstance(t, dict):
                continue
            content = str(t.get("content", "")).strip()
            if not content:
                continue
            due = t.get("due")
            due = due.strip() if isinstance(due, str) and due.strip().lower() not in ("", "null", "none") else None
            # A temporal item must expire: require a valid ISO due, else it would
            # sit active forever. Without one it is a durable fact, not temporal.
            if not due:
                u_notes.append(f"Item temporel sans échéance ignoré : {content[:60]}")
                continue
            try:
                datetime.date.fromisoformat(due[:10])
            except ValueError:
                u_notes.append(f"Item temporel à échéance invalide ignoré : {due!r}")
                continue
            stem = temporal.item_stem(content, due, None, temporal_taken | u_temporal)
            u_temporal.add(stem)
            rel, file_content = temporal.build_item(stem, str(t.get("type", "todo")), due, content)
            u_writes[rel] = file_content

        produced = bool(u_pages or u_temporal)
        # Apply every successful output, whatever else in the unit failed.
        writes.update(u_writes)
        new_desc.update(u_desc)
        page_paths |= u_pages
        temporal_taken |= u_temporal
        notes.extend(u_notes)
        if produced and not failed:
            consumed |= stm_stems
        elif failed:
            kept = ", ".join(n for n in (u.get("stm") or []) if isinstance(n, str))
            notes.append(f"Gardé en court terme (un élément a échoué, voir ci-dessus) : {kept}")
        else:
            kept = ", ".join(n for n in (u.get("stm") or []) if isinstance(n, str))
            notes.append(f"Gardé en court terme : {kept}")

    if page_paths or new_desc:
        writes["long_term/index.md"] = _regenerate_index(new_desc, page_paths)
        notes.append("Index régénéré.")

    if consumed:
        for stem in consumed:
            entry_rel = f"short_term/entries/{stem}.md"
            if resolve_under_root(entry_rel).is_file():
                deletes.append(entry_rel)
        writes["short_term/index.md"] = stm_index_content(exclude_stems=consumed)
        notes.append(f"Court terme consommé : {', '.join(sorted(consumed))}")

    expirations = temporal.expire_changes(day)
    writes.update(expirations)
    if expirations:
        notes.append(f"{len(expirations)} item(s) temporal expiré(s).")

    # Deduped summary of what landed (one line per unique page / item), prefixed
    # by the stage 1-2 plan so the execute report also shows the reasoning.
    applied: list[str] = []
    if page_paths:
        applied.append(f"{len(page_paths)} page(s) écrite(s) : " + ", ".join(sorted(page_paths)))
    temporal_made = sorted(f"temporal/{s}.md" for s in temporal_taken)
    if temporal_made:
        applied.append(f"{len(temporal_made)} item(s) temporel(s) : " + ", ".join(temporal_made))
    remaining = sorted({_stm_stem(name) for name, _ in stm_entries} - consumed)
    if remaining:
        applied.append(f"{len(remaining)} entrée(s) restée(s) en court terme : " + ", ".join(remaining))
    applied.extend(notes)
    plan = _format_decisions(pairs) if pairs else "Aucune décision."
    cost_lines = usage.cost_lines()
    cost = ("## Coût par étape\n\n" + "\n".join(f"- {c}" for c in cost_lines) + "\n\n") if cost_lines else ""
    report = (
        f"# Dream, {day}\n\n## Plan (étapes 1-2)\n\n{plan}\n\n"
        f"## Appliqué (étape 3)\n\n" + "\n".join(f"- {n}" for n in applied) + "\n\n"
        + cost
    )
    rel = f"{DREAM_REPORTS_DIR}/{day}.md"
    writes[rel] = report
    entry = usage.entry(when)
    if entry:
        writes[USAGE_FILE] = json.dumps(read_usage() + [entry], indent=2)
    apply_changes(writes, deletes, f"dream: {day} consolidation")
    return rel, report


def list_reports() -> list[str]:
    reports_dir = resolve_under_root(DREAM_REPORTS_DIR)
    if not reports_dir.is_dir():
        return []
    return [p.relative_to(wiki_root()).as_posix()
            for p in sorted(reports_dir.glob("*.md"), reverse=True)]
