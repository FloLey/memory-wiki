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

from wiki_server.paths import resolve_under_root
from wiki_server.store import write_file, write_files

# Serializes a whole dream run, so concurrent triggers cannot double-call the API
# or lose-update the usage ledger (read-append-write).
_dream_lock = threading.Lock()

DREAM_POLICY = "DREAM.md"
DREAM_REPORTS_DIR = "dream_reports"
USAGE_FILE = "dream_reports/usage.json"
DEFAULT_MODEL = "claude-opus-4-8"
# Estimated prices per 1M tokens, configurable per model via env. Token counts
# are exact (from the API); cost is an estimate based on these prices.
DEFAULT_PRICE_INPUT = 15.0
DEFAULT_PRICE_OUTPUT = 75.0

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
chaque groupe d'entrées liées : l'action choisie (integrer / promouvoir / garder),
la page cible (chemin), la justification en une ligne, et, le cas échéant, le
contenu rédigé proposé pour la page. Termine par les éventuelles réorganisations,
liens à créer, et mises à jour de l'index que tu proposerais."""


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


def _price(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name) or default)
    except (ValueError, TypeError):
        return default


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    price_in = _price("WIKI_DREAM_PRICE_INPUT", DEFAULT_PRICE_INPUT)
    price_out = _price("WIKI_DREAM_PRICE_OUTPUT", DEFAULT_PRICE_OUTPUT)
    return input_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out


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
                usage_entry = {
                    "timestamp": date.replace(microsecond=0).isoformat(),
                    "model": os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost": round(_estimate_cost(in_tok, out_tok), 6),
                }

        report = f"# Dream dry-run, {day}\n\n{body}\n"
        rel = f"{DREAM_REPORTS_DIR}/{day}-dryrun.md"
        files = {rel: report}
        if usage_entry is not None:
            files[USAGE_FILE] = json.dumps(read_usage() + [usage_entry], indent=2)
        write_files(files, f"dream: dry-run report {day}")
        return rel, report


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
