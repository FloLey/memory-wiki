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
import os

from wiki_server.paths import resolve_under_root
from wiki_server.store import write_file

DREAM_POLICY = "DREAM.md"
DREAM_REPORTS_DIR = "dream_reports"
DEFAULT_MODEL = "claude-opus-4-8"

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


def _ask_model(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "ANTHROPIC_API_KEY is not set; cannot run the dream. Add it as a secret."
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model = os.environ.get("WIKI_DREAM_MODEL") or DEFAULT_MODEL
    try:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")
    except Exception as exc:
        return f"The dream could not reach the model ({model}): {exc}"


def run_dry_run() -> tuple[str, str]:
    """Run a consolidation dry-run. Returns (report_relative_path, report_text).
    Writes the report into the wiki and commits it. Modifies nothing else."""
    policy = ensure_policy()
    stm_entries = _read_stm_entries()
    date = datetime.date.today().isoformat()

    if not stm_entries:
        body = "Short-term memory is empty. Nothing to consolidate."
    else:
        ltm_index = _read("long_term/index.md")
        body = _ask_model(_build_prompt(policy, stm_entries, ltm_index))

    report = f"# Dream dry-run, {date}\n\n{body}\n"
    rel = f"{DREAM_REPORTS_DIR}/{date}-dryrun.md"
    write_file(rel, report, f"dream: dry-run report {date}")
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
