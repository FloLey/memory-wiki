"""Personal Memory Wiki MCP server.

Read tools so far: a trivial connectivity check (``ping``) and the long-term
index reader (``read_long_term_index``).

Authentication (slice 2): when configured, the server is protected by GitHub
OAuth via FastMCP's ``GitHubProvider`` (an OAuth proxy that lets Claude.ai run
the standard OAuth 2.1 + PKCE discovery flow against an upstream GitHub OAuth
app). On top of "any valid GitHub login", an allow-list middleware restricts
access to a single GitHub account, so the wiki stays private to its owner.

Auth is controlled by environment:
- ``WIKI_AUTH_DISABLED=1`` runs the server open (local development only).
- Otherwise ``GH_OAUTH_CLIENT_ID`` and ``GH_OAUTH_CLIENT_SECRET`` are required,
  and the server fails loudly if they are missing (so production can never come
  up silently unauthenticated).

The ``/health`` route stays public regardless, for the container healthcheck.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from wiki_server import query
from wiki_server.paths import WikiPathError, resolve_under_root
from wiki_server.store import write_stm_entry
from wiki_server.ui import register_ui

DEFAULT_PUBLIC_URL = "https://wiki.florent-lejoly.be"
DEFAULT_ALLOWED_LOGIN = "FloLey"


def _build_auth():
    """Build the auth provider from the environment, or ``None`` when auth is
    explicitly disabled for local development."""
    if os.environ.get("WIKI_AUTH_DISABLED") == "1":
        return None

    # Imported lazily so the open/dev path has no hard dependency on the
    # provider stack.
    from fastmcp.server.auth.providers.github import GitHubProvider

    client_id = os.environ.get("GH_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GH_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Auth is enabled but GH_OAUTH_CLIENT_ID / GH_OAUTH_CLIENT_SECRET are "
            "not set. Set them, or set WIKI_AUTH_DISABLED=1 for local development."
        )

    kwargs = {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": os.environ.get("WIKI_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        "required_scopes": ["user"],
    }
    # A stable signing key keeps issued tokens valid across restarts/redeploys,
    # so the user is not forced to re-authorize on every deploy.
    signing_key = os.environ.get("WIKI_JWT_SIGNING_KEY")
    if signing_key:
        kwargs["jwt_signing_key"] = signing_key

    return GitHubProvider(**kwargs)


class AllowedUserMiddleware(Middleware):
    """Restrict every tool call to a single GitHub login.

    GitHub OAuth on its own lets *any* GitHub account through. This narrows it
    to the wiki owner by checking the ``login`` claim that the GitHub token
    verifier attaches to the access token.
    """

    def __init__(self, allowed_login: str):
        normalized = (allowed_login or "").strip().lower()
        if not normalized:
            # Fail closed: an empty allow-list, combined with an empty/missing
            # login claim, could otherwise let an unintended caller through.
            raise ValueError(
                "WIKI_ALLOWED_GITHUB_LOGIN must be a non-empty GitHub login."
            )
        self.allowed_login = normalized

    async def on_call_tool(self, context, call_next):
        token = get_access_token()
        if token is None:
            raise ToolError("Access denied: unauthenticated request.")
        claims = getattr(token, "claims", {}) or {}
        login = claims.get("login")
        # Deny on a missing/empty login too, never just on inequality.
        if not isinstance(login, str) or login.strip().lower() != self.allowed_login:
            raise ToolError("Access denied: this wiki is private to its owner.")
        return await call_next(context)


_auth = _build_auth()
mcp = FastMCP(name="personal-memory-wiki", auth=_auth)

if _auth is not None:
    _allowed_login = os.environ.get("WIKI_ALLOWED_GITHUB_LOGIN", DEFAULT_ALLOWED_LOGIN)
    mcp.add_middleware(AllowedUserMiddleware(_allowed_login))

# Browser console at /ui, gated by GitHub login restricted to the owner (or open
# in local dev when WIKI_AUTH_DISABLED=1). Reuses the same GitHub OAuth app.
register_ui(
    mcp,
    owner_login=os.environ.get("WIKI_ALLOWED_GITHUB_LOGIN", DEFAULT_ALLOWED_LOGIN),
    client_id=os.environ.get("GH_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GH_OAUTH_CLIENT_SECRET", ""),
    public_url=os.environ.get("WIKI_PUBLIC_URL", DEFAULT_PUBLIC_URL),
    secret_key=os.environ.get("WIKI_JWT_SIGNING_KEY", "dev-insecure-key"),
    auth_disabled=os.environ.get("WIKI_AUTH_DISABLED") == "1",
)


@mcp.tool
def prime() -> str:
    """Call this FIRST, at the very start of every conversation, before anything
    else. It returns the full text of the self pages (identity, style, voices,
    familiars) plus the long-term and short-term indexes in one call, so you know
    who the user is and the lay of the land. Then use search and read to go deeper.
    """
    return query.build_prime()


@mcp.tool
def read(path: str) -> str:
    """Read any file in the memory by its path: a page, an index, or a short-term
    entry. Examples: ``long_term/index.md``, ``long_term/self/identity.md``,
    ``short_term/index.md``, ``short_term/entries/1.md``.

    Tolerant of paths: a bare path like ``self/identity.md`` (as written in index
    links) is accepted. If nothing is found it returns suggestions, never a bare
    "does not exist", so never conclude a file is missing from a single attempt.
    """
    path = path.strip().strip("'\"")
    if not path:
        return "Error: empty path."
    # Try the path as given, then with the long_term/ prefix (index links are
    # written relative to long_term/, not the wiki root).
    candidates = [path]
    if not path.startswith(("long_term/", "short_term/")):
        candidates.append(f"long_term/{path}")
    for candidate in candidates:
        try:
            target = resolve_under_root(candidate)
        except WikiPathError:
            continue
        if target.is_file():
            try:
                return target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return f"Error reading {candidate}: {exc}"
    # Not found: suggest by filename, else list what exists. Never claim absence.
    pages = query.list_pages()
    matches = query.find_pages_by_name(path, pages)
    if matches:
        return f"No file at {path!r}. Did you mean: {', '.join(matches)} ?"
    listing = ", ".join(pages[:20]) if pages else "(none)"
    return f"No file at {path!r}. Existing pages: {listing}"


@mcp.tool
def search(query_text: str, max_results: int = 30) -> str:
    """Full-text search across the whole memory. Returns matching lines as
    ``path:line: text``. Use it to find where something is mentioned, then open
    the file with read.
    """
    return query.search_wiki(query_text, max_results=max_results)


@mcp.tool
def remember(
    content: str,
    summary: str = "",
    tags: list[str] | None = None,
    due: str | None = None,
    type: str | None = None,
) -> str:
    """Capture something into short-term memory. The nightly daemon sorts it
    later (into a long-term page, or into temporal/ if it is dated or actionable).

    Use it whenever the user mentions something worth keeping: a fact, a decision,
    a preference, a project, but also a task, a reminder, or an event. For dated or
    actionable items, set ``due`` (a date like 2026-06-15) and ``type`` (one of:
    todo, reminder, event). ``summary`` is an optional one-line label and ``tags``
    an optional list of short tags.
    """
    if not content.strip():
        raise ToolError("Nothing to remember: content is empty.")
    name, created = write_stm_entry(content, summary=summary, tags=tags, due=due, kind=type)
    return f"Remembered as short_term/entries/{name}.md ({created})."


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe used by the Docker healthcheck and deploy ``--wait``.
    Stays public (unauthenticated) on purpose."""
    return JSONResponse({"ok": True})


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    # Streamable HTTP transport at /mcp, the URL Claude.ai connects to.
    mcp.run(transport="http", host=host, port=port, path="/mcp")


if __name__ == "__main__":
    main()
