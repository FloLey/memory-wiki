"""Read and search helpers for the Personal Memory Wiki.

These back the MCP read tools that let Claude ground itself: a one-call context
loader (``build_prime``) and a full-text search (``search_wiki``). Search is a
pure-Python scan, which is plenty for a personal wiki of dozens of pages; it can
be swapped for ripgrep later if it ever feels slow.

The scan matches each whitespace-separated keyword independently (so word order
and phrasing do not matter), searches frontmatter ``tags`` as first-class
content, and tolerates typos via fuzzy matching. Pages that cover more of the
query rank first.
"""

from __future__ import annotations

import datetime
import difflib
import re

from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import parse_frontmatter

# Self pages (a fixed set), in the order Claude should read them to ground itself.
_SELF_ORDER = ["identity.md", "style.md", "voices.md"]

# Machinery hidden from the folder browser at the root (it has its own UI tabs).
_HIDDEN_AT_ROOT = {"dream_reports", "prompts", "DREAM.md",
                   "dream_models.json", "dream_schedule.json"}


def browse(rel: str):
    """List one directory level under the wiki root. Returns (subdirs, files):
    subdirs is [(name, relpath, md_count)], files is [(name, relpath)]. Skips
    dotfiles, the machinery at the root, and anything the path guard forbids (the
    private area). Raises WikiPathError if ``rel`` itself escapes the root.

    The per-folder page count reuses a single ``list_pages`` scan and counts by
    prefix in memory, so a listing is one disk scan, not one per subfolder."""
    base = resolve_under_root(rel) if rel else wiki_root()
    subdirs: list[tuple[str, str, int]] = []
    files: list[tuple[str, str]] = []
    if not base.is_dir():
        return subdirs, files
    all_pages = list_pages()
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        name = child.name
        if name.startswith("."):
            continue
        if not rel and name in _HIDDEN_AT_ROOT:
            continue
        child_rel = f"{rel}/{name}" if rel else name
        try:
            resolve_under_root(child_rel)
        except WikiPathError:
            continue
        if child.is_dir():
            prefix = f"{child_rel}/"
            count = sum(1 for p in all_pages if p.startswith(prefix))
            subdirs.append((name, child_rel, count))
        elif name.endswith(".md"):
            files.append((name, child_rel))
    return subdirs, files


def _safe_read(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _iter_wiki_md():
    """Yield (relative_path, Path) for every readable markdown file, skipping
    .git and the invisible long_term/private/ area."""
    root = wiki_root()
    private_dir = root / "long_term" / "private"
    git_dir = root / ".git"
    for p in sorted(root.rglob("*.md")):
        if p.is_relative_to(private_dir) or p.is_relative_to(git_dir):
            continue
        yield p.relative_to(root).as_posix(), p


def list_pages() -> list[str]:
    """All readable markdown page paths (relative to the wiki root)."""
    return [rel for rel, _ in _iter_wiki_md()]


def find_pages_by_name(name: str, pages: list[str] | None = None) -> list[str]:
    """Pages whose filename matches ``name`` (basename, case-insensitive).
    Lets a caller recover from a path missing its directory prefix. An optional
    pre-fetched ``pages`` list avoids a second disk scan."""
    base = name.rsplit("/", 1)[-1].strip().lower()
    if not base:
        return []
    if pages is None:
        pages = list_pages()
    return [rel for rel in pages if rel.rsplit("/", 1)[-1].lower() == base]


def build_prime() -> str:
    """Bundle the grounding context into one string: the self pages, then the
    long-term and short-term indexes."""
    sections: list[str] = []

    # Load exactly the fixed set of self pages, in order. Self is a closed set,
    # so we do not glob for others (a stray page stays reachable via read/search).
    self_dir = resolve_under_root("long_term/self")
    for name in _SELF_ORDER:
        sp = self_dir / name
        if sp.is_file():
            rel = sp.relative_to(wiki_root()).as_posix()
            sections.append(f"## {rel}\n\n{_safe_read(sp)}")

    for rel in ("long_term/index.md", "short_term/index.md"):
        p = resolve_under_root(rel)
        if p.is_file():
            sections.append(f"## {rel}\n\n{_safe_read(p)}")

    # Active temporal items. A past-due event is over (hidden); a past-due todo /
    # reminder stays, flagged OVERDUE, until it is marked done or long-past.
    from wiki_server import temporal

    today = datetime.date.today()
    lines = []
    for i in temporal.list_items(active_only=True):
        show, overdue = temporal.surface_state(i["meta"], today)
        if not show:
            continue
        flag = "EN RETARD " if overdue else ""
        first = i["body"].splitlines()[0] if i["body"] else ""
        lines.append(f"- {flag}[{i['meta'].get('type', 'item')}] {first}"
                     f" (due {i['meta'].get('due', '-')})")
    if lines:
        sections.append("## temporal (active)\n\n" + "\n".join(lines))

    if not sections:
        return "The wiki is empty."
    return "\n\n---\n\n".join(sections)


# --- search internals -------------------------------------------------------

# Fuzzy matching only kicks in for tokens this long: below it, near-matches are
# mostly noise (every short word is "close" to many others).
_FUZZY_MIN_LEN = 4
# difflib ratio a keyword/token pair must clear to count as a fuzzy hit. High
# enough that "flornet"~"florent" passes but unrelated words do not.
_FUZZY_THRESHOLD = 0.8

# Match quality, used to rank exact hits above fuzzy ones.
_EXACT = 2
_FUZZY = 1

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _keywords(query: str) -> list[str]:
    """Split a query into independent, lowercased keywords, de-duplicated
    (order preserved) so a repeated word is not matched and ranked twice."""
    return list(dict.fromkeys(k for k in query.lower().split() if k))


def _tags_of(meta: dict) -> list[str]:
    """Frontmatter tags as a list. Accepts ``[a, b]`` or ``a, b`` forms."""
    raw = meta.get("tags", "").strip().strip("[]").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


def _match_keywords(text: str, keywords: list[str]) -> dict[str, int]:
    """For one line (or the tag string), return {keyword: quality} for each
    keyword that hits it. A keyword hits by substring (exact) or, failing that,
    by being fuzzy-close to some word in the text (fuzzy)."""
    lowered = text.lower()
    tokens = None  # tokenize lazily; most lines never need the fuzzy pass
    hits: dict[str, int] = {}
    for kw in keywords:
        if kw in lowered:
            hits[kw] = _EXACT
            continue
        if len(kw) < _FUZZY_MIN_LEN:
            continue
        if tokens is None:
            tokens = _WORD_RE.findall(lowered)
        # Set the keyword as seq2 once so its char index is reused per token.
        matcher = difflib.SequenceMatcher()
        matcher.set_seq2(kw)
        for tok in tokens:
            if len(tok) < _FUZZY_MIN_LEN:
                continue
            # A ratio >= threshold needs 2*min(len) >= threshold*(sum of lens);
            # this length pre-filter skips clearly-too-different tokens cheaply.
            if 2 * min(len(kw), len(tok)) < _FUZZY_THRESHOLD * (len(kw) + len(tok)):
                continue
            matcher.set_seq1(tok)
            if (matcher.real_quick_ratio() >= _FUZZY_THRESHOLD
                    and matcher.quick_ratio() >= _FUZZY_THRESHOLD
                    and matcher.ratio() >= _FUZZY_THRESHOLD):
                hits[kw] = _FUZZY
                break
    return hits


def _frontmatter_span(lines: list[str]) -> int:
    """Number of leading lines occupied by a ``---`` frontmatter block (0 if
    none). Used so reported line numbers refer to the real file, not the body
    with frontmatter stripped."""
    if not lines or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx + 1
    return 0


def search_wiki(query: str, max_results: int = 30) -> str:
    """Keyword search across the whole memory. Each whitespace-separated keyword
    is matched independently (order-insensitive), frontmatter tags are searched
    as well as page text, and near-misses are caught by fuzzy matching so typos
    still hit. Returns matching lines as ``path:line: text`` (line ``0`` is a
    page's tags), ranked so pages covering more of the query come first, or a
    message when there is no match."""
    keywords = _keywords(query)
    if not keywords:
        return "Empty query."
    max_results = max(1, max_results)

    # (coverage, exact_hits, rel, [(lineno, text)]) per matching page.
    pages: list[tuple[int, int, str, list[tuple[int, str]]]] = []
    for rel, path in _iter_wiki_md():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        meta, _ = parse_frontmatter(text)

        covered: dict[str, int] = {}  # best quality seen per keyword, page-wide
        out_lines: list[tuple[int, str]] = []

        tags = _tags_of(meta)
        if tags:
            tag_str = ", ".join(tags)
            hits = _match_keywords(tag_str, keywords)
            if hits:
                for kw, q in hits.items():
                    covered[kw] = max(covered.get(kw, 0), q)
                out_lines.append((0, f"[tags: {tag_str}]"))

        skip = _frontmatter_span(lines)
        for i, line in enumerate(lines, 1):
            if i <= skip:
                continue
            hits = _match_keywords(line, keywords)
            if hits:
                for kw, q in hits.items():
                    covered[kw] = max(covered.get(kw, 0), q)
                out_lines.append((i, line.strip()))

        if covered:
            coverage = len(covered)
            exact_hits = sum(1 for q in covered.values() if q == _EXACT)
            pages.append((coverage, exact_hits, rel, out_lines))

    if not pages:
        return f"No matches for {query!r}."

    # More keywords covered first, then more exact (vs fuzzy) hits, then path.
    pages.sort(key=lambda p: (-p[0], -p[1], p[2]))

    results: list[str] = []
    truncated = False
    for _coverage, _exact, rel, out_lines in pages:
        for lineno, line in out_lines:
            if len(results) >= max_results:
                truncated = True  # a match exists beyond the cap
                break
            results.append(f"{rel}:{lineno}: {line}")
        if truncated:
            break
    if truncated:
        results.append(f"... (stopped at {max_results} matches)")
    return "\n".join(results)
