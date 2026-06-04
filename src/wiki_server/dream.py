"""Nightly consolidation daemon, dry-run phase.

Reads the policy (DREAM.md), short-term memory, and the long-term index, asks an
LLM for a consolidation plan, and writes a human-readable report to
``dream_reports/``. In dry-run it proposes only: it never modifies the memory.

The policy lives in the wiki at DREAM.md and is user-editable. A default is
shipped here and seeded into the wiki on first run only if absent (an edited
policy is never overwritten).
"""

from __future__ import annotations

import datetime
import json
import os
import threading

from wiki_server import temporal
from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import (
    apply_changes,
    stm_index_content,
    write_file,
    write_files,
)

# Serializes a whole dream run, so concurrent triggers cannot double-call the API
# or lose-update the usage ledger (read-append-write).
_dream_lock = threading.Lock()

DREAM_POLICY = "DREAM.md"
DREAM_REPORTS_DIR = "dream_reports"
USAGE_FILE = "dream_reports/usage.json"
DEFAULT_MODEL = "claude-opus-4-8"

# Anthropic list prices per 1M tokens (USD), by model tier (2026). Hardcoded so
# there is nothing to configure. Unknown / self-hosted models price at 0.
_PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

DEFAULT_DREAM_MD = """# DREAM.md

Tu es le consolidateur nocturne du Personal Memory Wiki de Florent. Une fois par
nuit, tu transformes la mémoire court terme en mémoire long terme. Ton seul cadre,
c'est ce fichier.

## Ce que tu lis
- Ce fichier (DREAM.md).
- La mémoire court terme : l'index et les entrées.
- L'index long terme (pour savoir ce qui existe et où ranger).

## Principe
La mémoire stocke de l'information, sobrement. Tu écris des notes factuelles,
claires, concises. Pas de style d'auteur, pas de voix, pas d'enjolivure. Neutre.

## Regrouper
Lis toutes les entrées court terme et regroupe-les par cohérence de sens (même
sujet, personne, projet, idée), pas par tags.

## Décider, pour chaque groupe
- Integrer : si le sujet a déjà une page long terme, fonds-y l'information
  (synthétise, ne duplique pas, évite les redites).
- Promouvoir : si c'est un sujet durable sans page, crée une nouvelle page dans
  la bonne catégorie.
- Garder : si ce n'est pas assez clair ou mûr, laisse l'entrée en court terme
  pour une prochaine nuit.
- Temporal : si une entrée est datée ou actionnable (tâche, rappel, événement
  borné, souvenir temporaire), range-la dans temporal/ plutôt qu'en long terme.
  La date "due" est la date JUSQU'À LAQUELLE l'item reste actif (date de fin pour
  un événement ou séjour borné, date limite pour une tâche). Passé cette date,
  l'item est archivé automatiquement.

Tu ne jettes jamais. Tu ne supprimes rien. Dans le doute, garde.

## Les cinq catégories (fixes)
self, entities, projects, concepts, sources. Tu ne crées jamais de nouvelle
catégorie de haut niveau ; tu ranges dedans.
- self : Florent lui-même.
- entities : personnes, lieux, organisations, objets.
- projects : ses projets.
- concepts : idées, sujets, savoirs.
- sources : livres, articles, références.

## Réorganiser (autorisé)
Tu peux créer des sous-dossiers, renommer, fusionner, déplacer des pages pour que
la structure reste claire (ex. regrouper plusieurs personnes sous entities/...).
Déplacer ou renommer n'est pas supprimer : tout reste réversible. Reste dans les
cinq catégories.

## Liens
Quand deux pages sont liées (une personne et un projet, par ex.), ajoute un lien
markdown de l'une vers l'autre, ex. [Fractaquin](../projects/fractaquin.md).
Quand tu déplaces ou renommes une page, mets à jour les liens qui pointaient
dessus : jamais de lien cassé.

## L'index
Tiens long_term/index.md à jour : ajoute les nouvelles pages avec une description
d'une ligne, corrige les chemins après un déplacement ou un renommage, sous la
bonne catégorie.

## Ton rapport
À la fin, écris un rapport factuel et bref de ce que tu as fait (ou, en dry-run,
de ce que tu ferais) : les groupes, l'action choisie, la cible, et pourquoi.

## Git
Tout le rêve est un seul commit préfixé dream:, avec un message qui résume la nuit.
"""


def ensure_policy() -> str:
    """Return the DREAM.md policy text, seeding the default into the wiki if it
    does not exist yet. Never overwrites an edited policy."""
    path = resolve_under_root(DREAM_POLICY)
    if not path.is_file():
        write_file(DREAM_POLICY, DEFAULT_DREAM_MD, "dream: add default DREAM.md policy")
    return path.read_text(encoding="utf-8")


def _read(rel: str) -> str:
    path = resolve_under_root(rel)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _read_stm_entries() -> list[tuple[str, str]]:
    entries_dir = resolve_under_root("short_term/entries")
    if not entries_dir.is_dir():
        return []
    paths = sorted(
        entries_dir.glob("*.md"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else 0,
    )
    return [(p.name, p.read_text(encoding="utf-8")) for p in paths]


def _build_prompt(policy: str, stm_entries: list[tuple[str, str]], ltm_index: str) -> str:
    entries_block = "\n\n".join(f"### short_term/entries/{name}\n{body}" for name, body in stm_entries)
    return f"""Voici ta politique de consolidation (DREAM.md). Suis-la strictement.

<policy>
{policy}
</policy>

Voici l'index de la mémoire long terme actuelle (ce qui existe déjà) :

<long_term_index>
{ltm_index or "(vide)"}
</long_term_index>

Voici les entrées de la mémoire court terme à consolider :

<short_term_entries>
{entries_block}
</short_term_entries>

Nous sommes en DRY-RUN : tu ne fais que PROPOSER, tu ne modifies rien.

Produis ton plan de consolidation sous forme de rapport markdown clair. Pour
chaque groupe d'entrées liées : l'action choisie (integrer / promouvoir / garder
/ temporal), la page ou la destination cible (chemin), la justification en une
ligne, et, le cas échéant, le contenu rédigé proposé. Pour une action temporal,
précise le type (todo / reminder / event / souvenir) et la date "due" = la date
jusqu'à laquelle l'item reste actif (date de fin pour un séjour borné). Termine
par les éventuelles réorganisations, liens à créer, et mises à jour de l'index."""


def _ask_model(prompt: str) -> tuple[str, int, int]:
    """Returns (text, input_tokens, output_tokens). Token counts are 0 when no
    real API call happened (missing key or error)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "ANTHROPIC_API_KEY is not set; cannot run the dream. Add it as a secret.", 0, 0
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model = os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL
    try:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = getattr(message, "usage", None)
        return text, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
    except Exception as exc:
        return f"The dream could not reach the model ({model}): {exc}", 0, 0


def _prices_for(model: str) -> tuple[float, float]:
    """(input, output) price per 1M tokens for a model, by tier. Unknown or
    self-hosted models return (0, 0) since there is no known API price."""
    name = (model or "").lower()
    for tier, prices in _PRICES.items():
        if tier in name:
            return prices
    return (0.0, 0.0)


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = _prices_for(model)
    return input_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out


def _usage_entry(when: datetime.datetime, in_tok: int, out_tok: int) -> dict:
    model = os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL
    return {
        "timestamp": when.replace(microsecond=0).isoformat(),
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost": round(_estimate_cost(model, in_tok, out_tok), 6),
    }


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
    """Aggregate cost/token stats across all dream runs."""
    entries = read_usage()
    total_cost = sum(float(e.get("cost", 0)) for e in entries)
    runs = len(entries)
    return {
        "runs": runs,
        "total_cost": total_cost,
        "last_cost": float(entries[-1].get("cost", 0)) if entries else 0.0,
        "avg_cost": total_cost / runs if runs else 0.0,
        "input_tokens": sum(int(e.get("input_tokens", 0)) for e in entries),
        "output_tokens": sum(int(e.get("output_tokens", 0)) for e in entries),
    }


def run_dry_run() -> tuple[str, str]:
    """Run a consolidation dry-run. Returns (report_relative_path, report_text).
    Writes the report (and a usage ledger entry) in one commit. Modifies nothing
    else."""
    with _dream_lock:
        policy = ensure_policy()
        stm_entries = _read_stm_entries()
        date = datetime.datetime.now(datetime.timezone.utc)
        day = date.date().isoformat()

        usage_entry = None
        if not stm_entries:
            body = "Short-term memory is empty. Nothing to consolidate."
        else:
            ltm_index = _read("long_term/index.md")
            body, in_tok, out_tok = _ask_model(_build_prompt(policy, stm_entries, ltm_index))
            if in_tok or out_tok:
                usage_entry = _usage_entry(date, in_tok, out_tok)

        report = f"# Dream dry-run, {day}\n\n{body}\n"
        rel = f"{DREAM_REPORTS_DIR}/{day}-dryrun.md"
        files = {rel: report}
        if usage_entry is not None:
            files[USAGE_FILE] = json.dumps(read_usage() + [usage_entry], indent=2)
        write_files(files, f"dream: dry-run report {day}")
        return rel, report


def _read_all_ltm_pages() -> list[tuple[str, str]]:
    """(path, content) for every long-term page (excluding private)."""
    root = wiki_root()
    ltm = root / "long_term"
    out = []
    if ltm.is_dir():
        for p in sorted(ltm.rglob("*.md")):
            if "private" in p.relative_to(root).parts:
                continue
            try:
                out.append((p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return out


def _build_execute_prompt(policy, stm_entries, ltm_pages, active_temporal) -> str:
    entries_block = "\n\n".join(f"### short_term/entries/{name}\n{body}" for name, body in stm_entries)
    pages_block = "\n\n".join(f"### {path}\n{content}" for path, content in ltm_pages) or "(aucune page)"
    temporal_block = "\n".join(
        f"- {i['path']} ({i['meta'].get('type','?')}, due {i['meta'].get('due','-')}): {i['body'][:80]}"
        for i in active_temporal
    ) or "(aucun)"
    return f"""Voici ta politique de consolidation (DREAM.md). Suis-la strictement.

<policy>
{policy}
</policy>

Pages long terme existantes (chemin puis contenu complet) :

<long_term_pages>
{pages_block}
</long_term_pages>

Items temporal actifs (déjà programmés, ne les recrée pas) :

<temporal_active>
{temporal_block}
</temporal_active>

Entrées de la mémoire court terme à consolider :

<short_term_entries>
{entries_block}
</short_term_entries>

Applique la politique et renvoie UNIQUEMENT un objet JSON (sans texte autour, sans
bloc de code) avec ce schéma :

{{
  "pages": [{{"path": "long_term/<categorie>/<nom>.md", "content": "<markdown COMPLET final de la page, liens markdown inclus>"}}],
  "index": "<contenu COMPLET final de long_term/index.md, à jour>",
  "temporal": [{{"type": "todo|reminder|event|souvenir", "due": "YYYY-MM-DD ou null", "content": "<texte>"}}],
  "consumed_stm": ["<nom de fichier des entrées court terme que tu as classées>"],
  "summary": "<résumé en une phrase>"
}}

Règles :
- Pour "integrer", donne le contenu COMPLET fusionné de la page existante (pas un diff).
- Pour "promouvoir", crée une nouvelle page sous une des cinq catégories.
- Les chemins de "pages" commencent par long_term/ et finissent par .md.
- "temporal" : les entrées datées ou actionnables vont là, pas en long terme.
  Le champ "due" est la date (YYYY-MM-DD) JUSQU'À LAQUELLE l'item reste actif :
  pour un événement ou séjour borné, mets la date de FIN ; pour une tâche, la date
  limite. Mets la plage lisible (ex. "du 26 au 30 juin") dans "content". Le fichier
  est nommé automatiquement, tu n'as pas à donner de chemin pour temporal.
- "consumed_stm" : les NOMS DE FICHIER des entrées court terme que tu as classées
  (tels qu'affichés ci-dessus, ex. 2026-06-03-voyage-venise). Celles que tu gardes
  (pas assez mûres) : ne les liste pas, elles restent en court terme.
- Tu ne supprimes rien d'autre.
"""


def _parse_plan(text: str) -> dict | None:
    """Extract the JSON plan from the model output. Returns None if unparseable.
    Tries fenced ```json blocks first, then a first-brace..last-brace fallback."""
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i].strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") and block.endswith("}"):
                try:
                    plan = json.loads(block)
                    if isinstance(plan, dict):
                        return plan
                except ValueError:
                    pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        plan = json.loads(cleaned[start:end + 1])
        return plan if isinstance(plan, dict) else None
    except ValueError:
        return None


def run_execute() -> tuple[str, str]:
    """Run a real consolidation: apply the model's plan (pages, temporal, index),
    drop consumed short-term entries, expire past-due temporal items, all in one
    commit. Never deletes long-term content. Returns (report_path, report_text)."""
    with _dream_lock:
        policy = ensure_policy()
        stm_entries = _read_stm_entries()
        when = datetime.datetime.now(datetime.timezone.utc)
        day = when.date().isoformat()

        writes: dict[str, str] = {}
        deletes: list[str] = []
        usage_entry = None
        notes: list[str] = []

        # Expire past-due temporal items regardless of STM.
        expirations = temporal.expire_changes(day)
        writes.update(expirations)
        if expirations:
            notes.append(f"Expired {len(expirations)} temporal item(s).")

        if not stm_entries:
            notes.append("Short-term memory is empty; nothing to consolidate.")
        else:
            ltm_pages = _read_all_ltm_pages()
            active_temporal = temporal.list_items(active_only=True)
            text, in_tok, out_tok = _ask_model(
                _build_execute_prompt(policy, stm_entries, ltm_pages, active_temporal)
            )
            if in_tok or out_tok:
                usage_entry = _usage_entry(when, in_tok, out_tok)
            plan = _parse_plan(text)
            if plan is None:
                notes.append("Could not parse a plan from the model; no changes applied.")
                notes.append(text[:500])
            else:
                _apply_plan(plan, writes, deletes, notes)

        report = f"# Dream, {day}\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
        rel = f"{DREAM_REPORTS_DIR}/{day}.md"
        writes[rel] = report
        if usage_entry is not None:
            writes[USAGE_FILE] = json.dumps(read_usage() + [usage_entry], indent=2)
        apply_changes(writes, deletes, f"dream: {day} consolidation")
        return rel, report


def _apply_plan(plan: dict, writes: dict, deletes: list, notes: list) -> None:
    """Translate a validated plan into writes/deletes. Skips anything unsafe."""
    # Pages (integrate + promote): full content, paths under long_term/.
    pages = plan.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            path = str(page.get("path", "")).strip()
            content = page.get("content", "")
            if not path.startswith("long_term/") or not path.endswith(".md") or not isinstance(content, str):
                notes.append(f"Skipped invalid page path: {path!r}")
                continue
            try:
                resolve_under_root(path)
            except WikiPathError:
                notes.append(f"Skipped forbidden page path: {path!r}")
                continue
            writes[path] = content
            notes.append(f"Wrote page {path}")

    # Index.
    index = plan.get("index")
    if isinstance(index, str) and index.strip():
        writes["long_term/index.md"] = index
        notes.append("Updated long_term/index.md")

    # Temporal items (descriptive, unique filenames).
    temporal_items = plan.get("temporal")
    if isinstance(temporal_items, list):
        taken: set[str] = set()
        for item in temporal_items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "todo"))
            due = item.get("due")
            due = due.strip() if isinstance(due, str) and due.strip().lower() not in ("", "null", "none") else None
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            stem = temporal.item_stem(content, due, None, taken)
            taken.add(stem)
            rel, file_content = temporal.build_item(stem, kind, due, content)
            writes[rel] = file_content
            notes.append(f"Created temporal {rel}")

    # Consumed short-term entries: delete files and rebuild the STM index. Guard
    # the type (a stray string would otherwise iterate into chars), and accept a
    # filename, a stem, or a full path.
    consumed_raw = plan.get("consumed_stm")
    consumed: set[str] = set()
    if isinstance(consumed_raw, list):
        for c in consumed_raw:
            if c is None:
                continue
            name = str(c).strip().split("/")[-1]
            name = name[:-3] if name.endswith(".md") else name
            if name:
                consumed.add(name)
    if consumed:
        for stem in consumed:
            entry_rel = f"short_term/entries/{stem}.md"
            if resolve_under_root(entry_rel).is_file():
                deletes.append(entry_rel)
        writes["short_term/index.md"] = stm_index_content(exclude_stems=consumed)
        notes.append(f"Consumed short-term entries: {', '.join(sorted(consumed))}")

    summary = plan.get("summary")
    if isinstance(summary, str) and summary.strip():
        notes.append(f"Summary: {summary.strip()}")


def list_reports() -> list[str]:
    """Existing dream report paths, newest first."""
    reports_dir = resolve_under_root(DREAM_REPORTS_DIR)
    if not reports_dir.is_dir():
        return []
    from wiki_server.paths import wiki_root

    return [
        p.relative_to(wiki_root()).as_posix()
        for p in sorted(reports_dir.glob("*.md"), reverse=True)
    ]
