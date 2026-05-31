"""Personal Memory Wiki MCP server.

Slice 1 (dummy MCP): a working remote MCP server that Claude.ai can connect to
over HTTPS. It exposes one trivial connectivity tool (``ping``) and one real
read tool (``read_long_term_index``) that reads the seeded long-term index.

Later slices add auth, short-term writing (``remember``), full-text search, the
remaining read tools, and the consolidation/digest daemons. Structural writes
to long-term memory are never exposed via MCP; they belong to the nightly
daemon only.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from wiki_server.paths import resolve_under_root

mcp = FastMCP("personal-memory-wiki")


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


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe used by the Docker healthcheck and deploy ``--wait``."""
    return JSONResponse({"ok": True})


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    # Streamable HTTP transport at /mcp, the URL Claude.ai connects to.
    mcp.run(transport="http", host=host, port=port, path="/mcp")


if __name__ == "__main__":
    main()
