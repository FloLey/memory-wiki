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

from wiki_server.paths import resolve_under_root
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
def ping(message: str = "pong") -> str:
    """Connectivity check. Echoes the message back so you can confirm the
    Personal Memory Wiki MCP server is reachable.
    """
    return f"personal-memory-wiki is alive: {message}"


@mcp.tool
def read_long_term_index() -> str:
    """Read the long-term memory catalog (``long_term/index.md``).

    This is the entry point for navigating the wiki: it lists the durable,
    curated knowledge pages by category. Read it first to know the terrain.
    """
    path = resolve_under_root("long_term/index.md")
    if not path.is_file():
        return "long_term/index.md does not exist yet."
    return path.read_text(encoding="utf-8")


@mcp.tool
def read_short_term_index() -> str:
    """Read the short-term memory index (``short_term/index.md``).

    A table of recent captures: id, time, a one-line summary, and tags. The
    summary is usually enough; open the full entry only when you need detail.
    """
    path = resolve_under_root("short_term/index.md")
    if not path.is_file():
        return "short_term/index.md does not exist yet."
    return path.read_text(encoding="utf-8")


@mcp.tool
def read_short_term_entry(entry_id: int) -> str:
    """Read the full text of a single short-term entry by its id."""
    path = resolve_under_root(f"short_term/entries/{entry_id}.md")
    if not path.is_file():
        return f"Short-term entry {entry_id} does not exist."
    return path.read_text(encoding="utf-8")


@mcp.tool
def remember(content: str, summary: str = "", tags: list[str] | None = None) -> str:
    """Capture something durable into short-term memory.

    Use this when the user mentions something worth keeping: a fact about them,
    a decision, a preference, an ongoing project. ``content`` is the text to
    store, ``summary`` is an optional one-line label for the index (derived from
    the first line if omitted), and ``tags`` is an optional list of short tags.
    """
    if not content.strip():
        raise ToolError("Nothing to remember: content is empty.")
    entry_id, created = write_stm_entry(content, summary=summary, tags=tags)
    return f"Remembered as short-term entry {entry_id} ({created})."


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
