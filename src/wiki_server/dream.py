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
DEFAULT_MODEL = "claude-opus-4-8"
_CATEGORIES = ["self", "entities", "projects", "concepts", "sources"]
_MAX_TOKENS = {"triage": 2048, "decide": 2048, "write": 8192}

# Anthropic list prices per 1M tokens (USD), by model tier (2026). Unknown /
# self-hosted models price at 0.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}

DEFAULT_DREAM_MD = """# DREAM.md

Tu es le consolidateur nocturne du Personal Memory Wiki de Florent. Une fois par
nuit, tu transformes la mémoire court terme en mémoire long terme. Ton seul cadre,
c'est ce fichier.

## Principe
La mémoire stocke de l'information, sobrement. Tu écris des notes factuelles,
claires, concises. Pas de style d'auteur, pas de voix, pas d'enjolivure. Neutre.

## Regrouper
Regroupe les entrées court terme par cohérence de sens (même sujet, personne,
projet, idée), pas par tags.

## Décider, pour chaque groupe
- Integrer : si le sujet a déjà une page long terme, fonds-y l'information
  (synthétise, ne duplique pas).
- Promouvoir : si c'est un sujet durable sans page, crée une nouvelle page dans
  la bonne catégorie.
- Garder : si ce n'est pas assez clair ou mûr, laisse l'entrée en court terme.
- Temporal : si une entrée est datée ou actionnable (tâche, rappel, événement
  borné, souvenir temporaire), range-la dans temporal/ plutôt qu'en long terme.
  La date "due" est la date JUSQU'À LAQUELLE l'item reste actif (date de fin pour
  un séjour borné, date limite pour une tâche). Passé cette date, il est archivé.

Tu ne jettes jamais. Tu ne supprimes rien. Dans le doute, garde.

## Les cinq catégories (fixes)
self, entities, projects, concepts, sources. Tu ne crées jamais de nouvelle
catégorie de haut niveau ; tu ranges dedans.
- self : Florent lui-même.
- entities : personnes, lieux, organisations, objets.
- projects : ses projets.
- concepts : idées, sujets, savoirs.
- sources : livres, articles, références.

## Liens
Quand deux pages sont liées, ajoute un lien markdown de l'une vers l'autre, ex.
[Fractaquin](../projects/fractaquin.md).
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

def _model_for(stage: str) -> str:
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


def _stage(usage: _Usage, stage: str, context: str) -> dict | None:
    """Run one pipeline stage: build the prompt, call the model, parse JSON.
    Never raises: any failure (import, client init, API, parsing) returns None."""
    try:
        import anthropic

        model = _model_for(stage)
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS.get(stage, 4096),
            messages=[{"role": "user", "content": prompts.build(stage, context)}],
        )
        text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")
        u = getattr(message, "usage", None)
        usage.add(model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
        return _parse_json(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _stm_stem(name: str) -> str:
    name = str(name).strip().split("/")[-1]
    return name[:-3] if name.endswith(".md") else name


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
        pages_block = "\n\n".join(f"### {t}\n{_read(t)}" for t in touches) or "(aucune page touchée)"
        decision = _stage(usage, "decide",
                          f"<policy>\n{policy}\n</policy>\n\n"
                          f"<unit intent=\"{unit.get('intent', '')}\">\n{unit_stm}\n</unit>\n\n"
                          f"<touched_pages>\n{pages_block}\n</touched_pages>")
        if decision:
            pairs.append({"unit": unit, "decision": decision})
        else:
            notes.append(f"Décision illisible pour : {unit.get('intent', names)}")
    return pairs


def _write_page(usage: _Usage, policy: str, decision: dict) -> dict | None:
    """Stage 3: produce {content, description} for an integrate/promote page."""
    page = decision.get("page", "")
    current = _read(page)
    out = _stage(usage, "write",
                 f"<policy>\n{policy}\n</policy>\n\n"
                 f"<decision>\n{json.dumps(decision, ensure_ascii=False)}\n</decision>\n\n"
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
    lines = ["# Long-term memory index", "", "Catalogue des pages durables, par catégorie.", ""]
    for cat in _CATEGORIES:
        lines.append(f"## {cat}")
        for full in sorted(p for p in paths if p.startswith(f"long_term/{cat}/")):
            rel_to_ltm = full[len("long_term/"):]
            stem = full.rsplit("/", 1)[-1][:-3]
            desc = descs.get(full, "")
            lines.append(f"- [{stem}]({rel_to_ltm})" + (f" : {desc}" if desc else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
        lines = [f"## {u.get('intent', '(unité)')}",
                 f"- entrées : {stm_names}",
                 f"- action : {d.get('action', '?')}"]
        if d.get("page"):
            lines.append(f"- cible : {d['page']}")
        if d.get("change"):
            lines.append(f"- changement : {d['change']}")
        t = d.get("temporal")
        if isinstance(t, dict) and t.get("content"):
            lines.append(f"- temporal : {t.get('type')} (due {t.get('due')}) : {t.get('content')}")
        if d.get("rationale"):
            lines.append(f"- pourquoi : {d['rationale']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_dry_run() -> tuple[str, str]:
    """Triage + decide, reported for review. Changes no memory."""
    with _dream_lock:
        when = datetime.datetime.now(datetime.timezone.utc)
        day = when.date().isoformat()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _no_key_report(day, dry=True)
        try:
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
        except Exception:
            return _error_report(day, dry=True)


def run_execute() -> tuple[str, str]:
    """Triage + decide + write, applied in one revertible commit."""
    with _dream_lock:
        when = datetime.datetime.now(datetime.timezone.utc)
        day = when.date().isoformat()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _no_key_report(day, dry=False)
        try:
            return _execute(when, day)
        except Exception:
            return _error_report(day, dry=False)


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
        action = d.get("action")
        stm_stems = {_stm_stem(n) for n in (u.get("stm") or []) if isinstance(n, str)}

        if action in ("integrate", "promote"):
            page = d.get("page")
            if not (isinstance(page, str) and page.startswith("long_term/") and page.endswith(".md")):
                notes.append(f"Décision ignorée (page invalide) : {page!r}")
                continue
            try:
                resolve_under_root(page)
            except WikiPathError:
                notes.append(f"Décision ignorée (page interdite) : {page!r}")
                continue
            written = _write_page(usage, policy, d)
            if not written:
                notes.append(f"Écriture échouée pour {page} ; entrée gardée.")
                continue
            writes[page] = written["content"]
            page_paths.add(page)
            if isinstance(written.get("description"), str) and written["description"].strip():
                new_desc[page] = written["description"].strip()
            consumed |= stm_stems
            notes.append(f"Page {page} ({action}).")
        elif action == "temporal":
            t = d.get("temporal") or {}
            content = str(t.get("content", "")).strip()
            if not content:
                notes.append("Temporal sans contenu, ignoré ; entrée gardée.")
                continue
            due = t.get("due")
            due = due.strip() if isinstance(due, str) and due.strip().lower() not in ("", "null", "none") else None
            stem = temporal.item_stem(content, due, None, temporal_taken)
            temporal_taken.add(stem)
            rel, file_content = temporal.build_item(stem, str(t.get("type", "todo")), due, content)
            writes[rel] = file_content
            consumed |= stm_stems
            notes.append(f"Temporal {rel}.")
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
