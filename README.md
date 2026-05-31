# Personal Memory Wiki

A persistent personal memory system for Claude. It accumulates, organizes, and
surfaces a single user's knowledge over time, built as a Python MCP server plus
nightly consolidation and weekly digest daemons. All data is plain Markdown in a
local git repo. No database, no embeddings, no RAG.

This project currently lives inside the `FloLey-public-website` repo, in this
self-contained `memory-wiki/` folder, so it can be lifted into its own repo
later with a folder copy. The code is path-agnostic via the `WIKI_ROOT`
environment variable.

## Status: Slice 1 (dummy MCP)

The goal of this slice is to prove the chain end to end: deploy a remote MCP
server, expose it over HTTPS, and connect Claude.ai to it. It is intentionally
small.

It exposes:

- `ping(message)` - connectivity check, echoes the message back.
- `read_long_term_index()` - reads the seeded `long_term/index.md`.
- `GET /health` - liveness probe for the Docker healthcheck.

There is **no authentication yet**. That is deliberate for this slice; OAuth is
the next step. Do not put sensitive content in the wiki until auth lands.

## Architecture

- **Transport:** Streamable HTTP, MCP mounted at `/mcp`, listening on `:8765`.
- **Data:** a Docker named volume `wiki_data` mounted at `/srv/wiki`. The
  entrypoint seeds it from `seed/` only if empty, then `git init`s it, so
  redeploys never clobber data.
- **Public URL:** `https://wiki.florent-lejoly.be/mcp`, fronted by Caddy
  (auto-HTTPS) in the main `docker-compose.yml`.

## Run locally

From the repo root:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build mcp-server
```

Then:

```sh
# Liveness
curl -s localhost:8765/health        # -> {"ok": true}

# Inspect tools and call them interactively (Streamable HTTP transport)
npx @modelcontextprotocol/inspector
#   connect to: http://localhost:8765/mcp
#   call ping, then read_long_term_index
```

## Connect Claude.ai (after deploy + DNS)

1. Make sure `wiki.florent-lejoly.be` resolves to the same public IP as
   `florent-lejoly.be` (one DNS record, see the deploy note below).
2. In Claude.ai: Settings -> Connectors -> Add custom connector.
3. URL: `https://wiki.florent-lejoly.be/mcp`. No auth for this slice.
4. Ask Claude to call `ping`, then `read_long_term_index()`.

## Deploy note (one manual prerequisite)

The build and deploy are automated by the repo's GitHub Actions workflow (a
`floley-public-website-mcp` image is built and pushed, then the VPS pulls it).
The only manual step is the DNS record for `wiki.florent-lejoly.be`. Caddy
issues HTTPS automatically on the first request once DNS resolves.

## Layout

```
memory-wiki/
  Dockerfile
  docker-entrypoint.sh    # seed-if-empty + git init, then run the server
  pyproject.toml
  src/wiki_server/
    server.py             # FastMCP app: tools + /health
    paths.py              # path validation under WIKI_ROOT, refuses private/
  seed/                   # initial wiki content, copied into the volume once
```

## Roadmap (next slices)

Auth (OAuth), `remember()` short-term writing, `search_wiki`, the remaining read
tools, the consolidation daemon, the weekly digest, and the web UI. See the full
specification for the phased plan.
