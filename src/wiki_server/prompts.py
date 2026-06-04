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

Regroupe les entrées court terme par cohérence de sens (même personne, projet,
lieu, sujet ou idée), pas par tags ni par date d'écriture. Pour chaque groupe,
indique :
- les fichiers court terme concernés,
- une intention courte et neutre,
- les pages long terme qu'il touche, comme une liste de chemins canoniques : pour
  un sujet déjà au catalogue, reprends son chemin tel quel ; pour un sujet
  nouveau, propose un chemin déterministe en minuscules-à-tirets
  (catégorie/slug). Un même sujet doit recevoir EXACTEMENT le même chemin dans
  toutes les unités qui le touchent, pour qu'une seule page existe au final,
- un indice d'action dominant : integrate (une page existante à enrichir),
  promote (un sujet durable sans page encore), temporal (une chose datée ou
  actionnable), keep (pas assez mûr, à laisser en court terme).

L'indice est une tendance, pas une contrainte : un même groupe peut avoir une
part durable et une part datée, et l'étape suivante pourra produire les deux.
Sois sélectif et fidèle ; n'invente rien et ne déduis pas au-delà du texte.
"""

DECIDE_DEFAULT = """# Prompt: decide (étape 2)

Tu décides quoi faire d'UNE unité. On te donne la politique (DREAM.md), l'unité
(les textes court terme), et le contenu actuel des pages long terme qu'elle
touche.

Une entrée peut concerner plusieurs sujets : liste une opération par page long
terme touchée (integrate pour une page existante, promote pour une nouvelle), et
un item par chose datée. Une entrée qui mentionne plusieurs personnes, projets ou
sujets touche donc une page par sujet, pas une seule. Reprends les chemins exacts
fournis dans touched_pages comme chemins canoniques, sans inventer de variante :
integrate si la page a déjà du contenu, promote si elle est vide (nouvelle). Si
rien n'est assez mûr,
laisse l'entrée en court terme (listes vides). Pour les items temporels : type
(todo/reminder/event) et "due" OBLIGATOIRE = la date (YYYY-MM-DD) jusqu'à laquelle
l'item reste actif (échéance d'une tâche, date de fin d'un séjour). Un item
temporel a toujours une fin : s'il n'a pas de date d'expiration, ce n'est pas un
item temporel mais un fait durable, qui va alors sur une page long terme (avec sa
date dans le texte), jamais dans temporal/. Ne sur-fusionne pas : plusieurs choses
datées distinctes dans une même unité donnent un item temporel chacune, avec sa
propre échéance.

Sépare le fait durable de son emballage daté. Une chose datée mentionne souvent
un fait qui, lui, est durable : une personne, une relation, un lieu, une
préférence. Produis une page (promote ou integrate) pour la partie durable, et un
item temporel pour la partie datée seulement si elle a une date de fin. Un
événement marquant mais sans échéance (une naissance, une rencontre) est un fait
durable : il va sur la page concernée, pas dans temporal/. Une personne ou une
relation qui revient mérite sa page même si elle n'apparaît que dans des choses
datées. Tu ne supprimes jamais.
"""

WRITE_DEFAULT = """# Prompt: write (étape 3)

Tu écris UNE page de la mémoire long terme. On te donne la politique (DREAM.md),
l'opération à appliquer, et le contenu actuel de la page (si elle existe déjà).

Écris des notes factuelles, claires et concises. Pas de style d'auteur, pas de
voix narrative, ton neutre. Pour une intégration, renvoie le contenu COMPLET
fusionné (pas un diff), sans rien dupliquer ni perdre de l'existant. Pour une
nouvelle page, structure l'essentiel sans la gonfler. Inclus les liens markdown
utiles vers les pages connexes. Donne aussi une description d'une ligne pour
l'index. N'invente rien qui ne soit pas dans la source ou la page actuelle.
"""

DEFAULTS = {"triage": TRIAGE_DEFAULT, "decide": DECIDE_DEFAULT, "write": WRITE_DEFAULT}

# Output contracts, controlled by code (always appended to the guidance).
SCHEMAS = {
    "triage": (
        'Renvoie UNIQUEMENT cet objet JSON, sans texte autour ni bloc de code :\n'
        '{"units": [{"stm": ["<nom de fichier court terme>"], "intent": "<courte description>", '
        '"touches": ["long_term/<chemin>.md"], "hint": "integrate|promote|temporal|keep"}]}\n'
        '"touches" liste les chemins canoniques des pages touchées, existants ou '
        'proposés pour un sujet nouveau ; un même sujet reçoit le même chemin dans '
        'toutes les unités.'
    ),
    "decide": (
        'Renvoie UNIQUEMENT cet objet JSON, sans texte autour ni bloc de code :\n'
        '{"pages": [{"action": "integrate|promote", "page": "long_term/<chemin>.md", '
        '"change": "<ce qu\'on ajoute/fusionne>"}], '
        '"temporal": [{"type": "todo|reminder|event", "due": "YYYY-MM-DD", "content": "<texte>"}], '
        '"rationale": "<courte justification>"}\n'
        '"pages" et "temporal" sont des LISTES. Une entrée peut toucher plusieurs pages '
        '(personnes, projets, sujets) et/ou plusieurs choses datées : mets une opération '
        'par page (integrate pour une page existante, promote pour une nouvelle) et un '
        'item par chose datée. Chaque item temporel a une "due" (YYYY-MM-DD) obligatoire ; '
        'sans échéance, ce n\'est pas un item temporel mais un fait durable (une page). '
        'Tout vide = garder l\'entrée en court terme.'
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
