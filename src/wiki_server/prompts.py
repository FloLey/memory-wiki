"""Editable prompt guidance for the three dream stages (triage, decide, write).

Each prompt has two parts: the *guidance* (how to think), stored as an editable
file in the wiki and seeded with a default here; and the *schema* (the exact JSON
the code parses), kept in code and always appended by the daemon so editing the
guidance can never break the machine contract. A missing/edited file falls back
to nothing special: the code seeds the default if absent.
"""

from __future__ import annotations

from wiki_server.paths import resolve_under_root
from wiki_server.store import write_file

PROMPT_FILES = {
    "triage": "prompts/triage.md",
    "decide": "prompts/decide.md",
    "write": "prompts/write.md",
}

TRIAGE_DEFAULT = """# Prompt: triage (étape 1)

Tu es le trieur du Personal Memory Wiki. On te donne la politique (DREAM.md),
toute la mémoire court terme, et l'index de la mémoire long terme (le catalogue,
pas le contenu des pages).

Regroupe les entrées court terme par cohérence de sens (même sujet, personne,
projet, idée), pas par tags. Pour chaque groupe, indique :
- les fichiers court terme concernés,
- une intention courte,
- les pages long terme existantes qu'il touche (chemins tirés de l'index, ou
  vide si c'est un sujet nouveau),
- un indice d'action (integrate / promote / temporal / keep).

Sois sélectif et fidèle ; n'invente rien.
"""

DECIDE_DEFAULT = """# Prompt: decide (étape 2)

Tu décides quoi faire d'UNE unité. On te donne la politique (DREAM.md), l'unité
(les textes court terme), et le contenu actuel des pages long terme qu'elle
touche.

Choisis une action : integrate (fondre dans une page existante), promote (créer
une nouvelle page dans la bonne catégorie), temporal (chose datée ou
actionnable), ou keep (laisser en court terme si pas assez mûr). Donne la page
cible, ce que tu changes (en prose, pour relecture), les liens éventuels, et une
justification courte. Pour temporal : type (todo/reminder/event/souvenir) et
"due" = la date jusqu'à laquelle l'item reste actif (date de fin pour un séjour
borné). Tu ne supprimes jamais.
"""

WRITE_DEFAULT = """# Prompt: write (étape 3)

Tu écris UNE page de la mémoire long terme. On te donne la politique (DREAM.md),
la décision, et le contenu actuel de la page (si on intègre).

Écris des notes factuelles, claires, concises. Pas de style d'auteur, pas de
voix, neutre. Pour une intégration, produis le contenu COMPLET fusionné (pas un
diff), sans dupliquer ni perdre d'information. Inclus les liens markdown utiles.
Donne aussi une description d'une ligne de la page, pour l'index.
"""

DEFAULTS = {"triage": TRIAGE_DEFAULT, "decide": DECIDE_DEFAULT, "write": WRITE_DEFAULT}

# Output contracts, controlled by code (always appended to the guidance).
SCHEMAS = {
    "triage": (
        'Renvoie UNIQUEMENT cet objet JSON, sans texte autour ni bloc de code :\n'
        '{"units": [{"stm": ["<nom de fichier court terme>"], "intent": "<courte description>", '
        '"touches": ["long_term/<chemin>.md"], "hint": "integrate|promote|temporal|keep"}]}'
    ),
    "decide": (
        'Renvoie UNIQUEMENT cet objet JSON, sans texte autour ni bloc de code :\n'
        '{"action": "integrate|promote|temporal|keep", "page": "long_term/<chemin>.md", '
        '"change": "<ce qu\'on ajoute/fusionne>", '
        '"temporal": {"type": "todo|reminder|event|souvenir", "due": "YYYY-MM-DD ou null", "content": "<texte>"}, '
        '"links": ["long_term/<chemin>.md"], "rationale": "<courte justification>"}\n'
        'Mets "temporal" à null si l\'action n\'est pas temporal. "page" à null pour keep/temporal.'
    ),
    "write": (
        'Renvoie UNIQUEMENT cet objet JSON, sans texte autour ni bloc de code :\n'
        '{"content": "<markdown COMPLET final de la page, liens inclus>", '
        '"description": "<une ligne pour l\'index>"}'
    ),
}


def ensure_prompt(stage: str) -> str:
    """Return the editable guidance for a stage, seeding the default if absent."""
    rel = PROMPT_FILES[stage]
    path = resolve_under_root(rel)
    if not path.is_file():
        write_file(rel, DEFAULTS[stage], f"dream: add default {rel}")
    return path.read_text(encoding="utf-8")


def build(stage: str, context: str) -> str:
    """Full prompt = editable guidance + injected context + fixed JSON schema."""
    guidance = ensure_prompt(stage)
    return f"{guidance}\n\n{context}\n\n{SCHEMAS[stage]}"
