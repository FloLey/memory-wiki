"""Nightly consolidation daemon.

A three-stage pipeline so no single call ever needs the whole long-term memory:
1. triage  : cluster short-term memory and route each unit (input: policy + all
   short-term + the long-term index).
2. decide  : per unit, read only the touched pages and decide the change.
3. write   : per integrate/promote decision, produce the page's final content.

Dry-run stops after stage 2 and reports the decisions (changes nothing). Execute
runs stage 3 and applies everything in one revertible commit: it never deletes
long-term content, regenerates the index, drops consumed short-term entries, and
expires past-due temporal items.

The policy (DREAM.md) and the three stage prompts are editable files in the wiki;
the JSON schemas are injected by code so editing the guidance cannot break the
machine contract.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading

from wiki_server import prompts, temporal
from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import apply_changes, stm_index_content, write_files

_dream_lock = threading.Lock()

DREAM_POLICY = "DREAM.md"
DREAM_REPORTS_DIR = "dream_reports"
USAGE_FILE = "dream_reports/usage.json"
MODELS_FILE = "dream_models.json"
DEFAULT_MODEL = "claude-opus-4-8"
STAGES = ("triage", "decide", "write")
# Models offered in the UI, cheapest-impacting first. Label carries the list
# price (per 1M tokens, in/out) so the cost trade-off is visible when choosing.
AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku 4.5 : rapide, le moins cher (1 / 5 $/M)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 : bon compromis (3 / 15 $/M)"),
    ("claude-opus-4-8", "Opus 4.8 : le plus capable, le plus cher (5 / 25 $/M)"),
]
_MODEL_IDS = {m for m, _ in AVAILABLE_MODELS}
_CATEGORIES = ["self", "people", "places", "organizations", "projects", "concepts", "sources"]
_MAX_TOKENS = {"triage": 2048, "decide": 2048, "write": 8192}

# Anthropic list prices per 1M tokens (USD), by model tier (2026). Unknown /
# self-hosted models price at 0.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}

DEFAULT_DREAM_MD = """# DREAM.md

Tu es le consolidateur nocturne du Personal Memory Wiki de Florent. Une fois par
nuit, tu transformes la mémoire court terme en mémoire long terme. Ce fichier est
le cadre commun à toutes les étapes ; chaque étape a en plus ses propres
consignes, qu'il ne répète pas ici.

## Principe
La mémoire stocke de l'information, sobrement : notes factuelles, claires,
concises. Pas de style d'auteur, pas de voix, pas d'enjolivure, ton neutre. Tu ne
jettes jamais, tu ne supprimes rien. Dans le doute, garde.

## Les catégories (fixes)
Tu ne crées jamais de nouvelle catégorie de haut niveau ; tu ranges dans l'une
de celles-ci.
- self : Florent lui-même. Une seule page, self/identity.md, regroupe tous les
  faits durables sur lui (naissance, métier, etc.) ; tu intègres ces faits dans
  cette page unique et n'éclates jamais ses attributs en plusieurs pages self.
- people : les personnes (famille, amis, collègues, connaissances).
- places : les lieux.
- organizations : les organisations et entreprises.
- projects : ses projets.
- concepts : idées, sujets, savoirs.
- sources : livres, articles, références.

Une personne, un lieu ou une organisation lié à un fait garde sa propre page
dans sa catégorie, reliée par un lien.

## Liens
Quand deux pages sont liées, relie-les par un lien markdown en chemin relatif,
de la forme `[<titre de la page>](../<catégorie>/<nom>.md)`.
"""


def ensure_policy() -> str:
    path = resolve_under_root(DREAM_POLICY)
    if not path.is_file():
        from wiki_server.store import write_file

        write_file(DREAM_POLICY, DEFAULT_DREAM_MD, "dream: add default DREAM.md policy")
    return path.read_text(encoding="utf-8")


def _read(rel: str) -> str:
    path = resolve_under_root(rel)
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""


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


# ---------------------------------------------------------------------------
# Model calls and cost
# ---------------------------------------------------------------------------

def read_models() -> dict[str, str]:
    """Per-stage model overrides chosen in the UI, stored in the wiki. Only
    valid {stage: known-model} pairs are returned; anything else is ignored."""
    path = resolve_under_root(MODELS_FILE)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {s: data[s] for s in STAGES
            if isinstance(data.get(s), str) and data[s] in _MODEL_IDS}


def set_models(mapping: dict[str, str]) -> dict[str, str]:
    """Persist the per-stage model choices, keeping only valid pairs, and return
    what was stored. Committed like any other wiki change."""
    from wiki_server.store import write_file

    chosen = {s: mapping[s] for s in STAGES
              if isinstance(mapping.get(s), str) and mapping[s] in _MODEL_IDS}
    write_file(MODELS_FILE, json.dumps(chosen, indent=2) + "\n",
               "manual: set dream models")
    return chosen


def effective_models() -> dict[str, str]:
    """The model that will actually run for each stage (UI choice first)."""
    return {s: _model_for(s) for s in STAGES}


def _model_for(stage: str) -> str:
    # UI choice (wiki file) wins, then env overrides, then the default.
    chosen = read_models().get(stage)
    if chosen:
        return chosen
    return (os.environ.get(f"WIKI_DREAM_MODEL_{stage.upper()}")
            or os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL)


def _prices_for(model: str) -> tuple[float, float]:
    name = (model or "").lower()
    for tier, prices in _PRICES.items():
        if tier in name:
            return prices
    return (0.0, 0.0)


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    price_in, price_out = _prices_for(model)
    return in_tok / 1_000_000 * price_in + out_tok / 1_000_000 * price_out


class _Usage:
    """Accumulates token usage and cost across the pipeline's calls (which may
    use different models per stage)."""

    def __init__(self) -> None:
        self.in_tok = 0
        self.out_tok = 0
        self.cost = 0.0
        self.models: set[str] = set()

    def add(self, model: str, in_tok: int, out_tok: int) -> None:
        self.in_tok += in_tok
        self.out_tok += out_tok
        self.cost += _estimate_cost(model, in_tok, out_tok)
        self.models.add(model)

    def entry(self, when: datetime.datetime) -> dict | None:
        if not (self.in_tok or self.out_tok):
            return None
        return {
            "timestamp": when.replace(microsecond=0).isoformat(),
            "model": ", ".join(sorted(self.models)) or DEFAULT_MODEL,
            "input_tokens": self.in_tok,
            "output_tokens": self.out_tok,
            "cost": round(self.cost, 6),
        }


def _parse_json(text: str) -> dict | None:
    """Extract a JSON object from model output. None if unparseable."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i].strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") and block.endswith("}"):
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _call_model(usage: _Usage, model: str, prompt: str, max_tokens: int) -> str | None:
    """Call the model once and return its text. Never raises: any failure
    (import, client init, API) returns None. Records token usage."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        u = getattr(message, "usage", None)
        usage.add(model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
        return text
    except Exception:
        return None


def _stage(usage: _Usage, stage: str, context: str) -> dict | None:
    """Run one pipeline stage: build the prompt, call the model, parse JSON.
    Returns None on any failure."""
    text = _call_model(usage, _model_for(stage), prompts.build(stage, context),
                       _MAX_TOKENS.get(stage, 4096))
    return _parse_json(text) if text is not None else None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Index regeneration
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Usage ledger
# ---------------------------------------------------------------------------

def read_usage() -> list[dict]:
    path = resolve_under_root(USAGE_FILE)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def reset_usage() -> None:
    """Clear the cost ledger so tracking starts over. The history stays in git."""
    write_files({USAGE_FILE: "[]\n"}, "manual: reset dream cost ledger")


# ---------------------------------------------------------------------------
# One-shot migration: split the legacy ``entities`` folder into people / places
# / organizations (when the taxonomy changed). Atomic, git-reversible.
# ---------------------------------------------------------------------------

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
    data = _parse_json(_call_model(usage, _model_for("triage"), prompt, 1024) or "") or {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v in _MIGRATION_CATEGORIES}


def _guarded(dry: bool, work) -> tuple[str, str]:
    """Run a dream entry point under the lock. Never raises: returns a report if
    the API key is missing or the work fails, so the UI never gets a raw 500."""
    with _dream_lock:
        when = datetime.datetime.now(datetime.timezone.utc)
        day = when.date().isoformat()
        try:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return _no_key_report(day, dry)
            return work(when, day)
        except Exception:
            return _error_report(day, dry)


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


def usage_summary() -> dict:
    entries = read_usage()
    total = sum(float(e.get("cost", 0)) for e in entries)
    runs = len(entries)
    return {
        "runs": runs,
        "total_cost": total,
        "last_cost": float(entries[-1].get("cost", 0)) if entries else 0.0,
        "avg_cost": total / runs if runs else 0.0,
        "input_tokens": sum(int(e.get("input_tokens", 0)) for e in entries),
        "output_tokens": sum(int(e.get("output_tokens", 0)) for e in entries),
    }


# ---------------------------------------------------------------------------
# Dry-run and execute
# ---------------------------------------------------------------------------

def _no_key_report(day: str, dry: bool) -> tuple[str, str]:
    body = "ANTHROPIC_API_KEY is not set; cannot run the dream. Add it as a secret."
    suffix = "-dryrun" if dry else ""
    rel = f"{DREAM_REPORTS_DIR}/{day}{suffix}.md"
    report = f"# Dream {'dry-run' if dry else ''}, {day}\n\n{body}\n"
    write_files({rel: report}, f"dream: report {day}")
    return rel, report


def _error_report(day: str, dry: bool) -> tuple[str, str]:
    """Capture an unexpected failure into a report instead of crashing the
    request, so the user sees the cause and the dream never returns a raw 500."""
    import traceback

    tb = traceback.format_exc()
    suffix = "-dryrun" if dry else ""
    rel = f"{DREAM_REPORTS_DIR}/{day}{suffix}.md"
    report = (
        f"# Dream {'dry-run' if dry else ''}, {day}\n\n"
        f"Le rêve a échoué. Trace technique :\n\n```\n{tb}\n```\n"
    )
    try:
        write_files({rel: report}, f"dream: error report {day}")
    except Exception:
        pass
    return rel, report


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
            u_notes.append(f"Page {page} ({op.get('action', 'write')}).")

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
            u_notes.append(f"Temporal {rel}.")

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

    report = f"# Dream, {day}\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
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
