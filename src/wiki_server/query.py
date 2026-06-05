"""Read and search helpers for the Personal Memory Wiki.

These back the MCP read tools that let Claude ground itself: a one-call context
loader (``build_prime``) and a full-text search (``search_wiki``). Search is a
simple pure-Python scan, which is plenty for a personal wiki of dozens of pages;
it can be swapped for ripgrep later if it ever feels slow.
"""

from __future__ import annotations

import datetime

from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root

# Self pages, in the order Claude should read them to ground itself.
_SELF_ORDER = ["identity.md", "style.md", "voices.md", "familiars.md"]

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

    self_dir = resolve_under_root("long_term/self")
    if self_dir.is_dir():
        seen = set()
        ordered = []
        for name in _SELF_ORDER:
            sp = self_dir / name
            if sp.is_file():
                ordered.append(sp)
                seen.add(sp.name)
        for sp in sorted(self_dir.glob("*.md")):
            if sp.name not in seen:
                ordered.append(sp)
        for sp in ordered:
            rel = sp.relative_to(wiki_root()).as_posix()
            sections.append(f"## {rel}\n\n{_safe_read(sp)}")

    for rel in ("long_term/index.md", "short_term/index.md"):
        p = resolve_under_root(rel)
        if p.is_file():
            sections.append(f"## {rel}\n\n{_safe_read(p)}")

    # Active temporal items (todos, reminders, events). Also hide anything whose
    # due (active-until) is already past, even if no dream has expired it yet, so
    # what Claude sees is only what is still current.
    from wiki_server import temporal

    today = datetime.date.today()

    def _still_current(item: dict) -> bool:
        due = item["meta"].get("due", "")
        try:
            return datetime.date.fromisoformat(str(due)[:10]) >= today
        except ValueError:
            return True  # missing or malformed due: cannot date it, keep it

    active = [i for i in temporal.list_items(active_only=True) if _still_current(i)]
    if active:
        lines = [
            f"- [{i['meta'].get('type', 'item')}] {i['body'].splitlines()[0] if i['body'] else ''}"
            f" (due {i['meta'].get('due', '-')})"
            for i in active
        ]
        sections.append("## temporal (active)\n\n" + "\n".join(lines))

    if not sections:
        return "The wiki is empty."
    return "\n\n---\n\n".join(sections)


def search_wiki(query: str, max_results: int = 30) -> str:
    """Case-insensitive full-text search. Returns matching lines as
    ``path:line: text``, or a message when there is no match."""
    needle = query.strip().lower()
    if not needle:
        return "Empty query."
    max_results = max(1, max_results)
    results: list[str] = []
    for rel, path in _iter_wiki_md():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, 1):
            if needle in line.lower():
                results.append(f"{rel}:{i}: {line.strip()}")
                if len(results) >= max_results:
                    results.append(f"... (stopped at {max_results} matches)")
                    return "\n".join(results)
    return "\n".join(results) if results else f"No matches for {query!r}."
