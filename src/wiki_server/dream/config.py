"""Shared configuration and low-level helpers for the dream package.

Constants, the editable policy default (DREAM.md), and the small filesystem
reader used across the pipeline, index and migration steps.
"""

from __future__ import annotations

import threading

from wiki_server.paths import resolve_under_root

# Serializes a whole dream run (one entry point at a time): the pipeline reads
# and rewrites many files, so two concurrent runs must not interleave.
_dream_lock = threading.Lock()

DREAM_POLICY = "DREAM.md"
DREAM_REPORTS_DIR = "dream_reports"
USAGE_FILE = "dream_reports/usage.json"

# Fixed top-level long-term categories. Pages are filed under one of these and
# the index is grouped by them; new top-level categories are never created.
_CATEGORIES = ["self", "people", "places", "organizations", "projects", "concepts", "sources"]

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
    """Return the editable policy, seeding the default on first use."""
    path = resolve_under_root(DREAM_POLICY)
    if not path.is_file():
        from wiki_server.store import write_file

        write_file(DREAM_POLICY, DEFAULT_DREAM_MD, "dream: add default DREAM.md policy")
    return path.read_text(encoding="utf-8")


def _read(rel: str) -> str:
    """Read a wiki file, returning '' if it is missing or unreadable."""
    path = resolve_under_root(rel)
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""
