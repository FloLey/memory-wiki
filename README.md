# Personal Memory Wiki

A persistent personal memory system for Claude. It accumulates, organizes, and
surfaces a single user's knowledge over time, built as a Python MCP server plus
nightly consolidation and weekly digest daemons. All data is plain Markdown in a
local git repo. No database, no embeddings, no RAG.

This project currently lives inside the `FloLey-public-website` repo, in this
self-contained `memory-wiki/` folder, so it can be lifted into its own repo
later with a folder copy. The code is path-agnostic via the `WIKI_ROOT`
environment variable.

## Status: Slice 3 (short-term memory)

Slice 1 proved the chain end to end (remote MCP server over HTTPS, connected to
Claude.ai). Slice 2 closed the open door with GitHub OAuth. Slice 3 makes the
wiki start living: Claude can write to short-term memory and read it back.

It exposes:

- `ping(message)` - connectivity check, echoes the message back.
- `read_long_term_index()` - reads `long_term/index.md`.
- `read_short_term_index()` - reads the short-term index table.
- `read_short_term_entry(id)` - reads one short-term entry in full.
- `remember(content, summary?, tags?)` - captures something into short-term
  memory: writes `short_term/entries/{id}.md`, appends a row to the index, and
  commits with a `stm:` prefix.
- `GET /health` - liveness probe for the Docker healthcheck.

Short-term memory is the open, fast-to-write layer. It accumulates as you talk;
a later consolidation phase will distil it into curated long-term pages. Writing
is intentionally the only write path exposed over MCP: structural long-term
edits belong to the (future) nightly daemon, not to a live conversation.

## Authentication (slice 2)

The server is protected by **GitHub OAuth** via FastMCP's `GitHubProvider` (an
OAuth proxy that runs the standard OAuth 2.1 + PKCE discovery flow Claude.ai
expects). On top of "any valid GitHub login", an allow-list middleware
(`AllowedUserMiddleware`) restricts access to a single GitHub account
(`WIKI_ALLOWED_GITHUB_LOGIN`), so the wiki stays private to its owner.

Controlled by environment:

- `WIKI_AUTH_DISABLED=1` runs the server open. The **dev compose sets this**, so
  local development needs no secrets.
- Otherwise `GH_OAUTH_CLIENT_ID` and `GH_OAUTH_CLIENT_SECRET` are **required**;
  the server refuses to start without them (production can never come up
  silently open). `WIKI_JWT_SIGNING_KEY` should be set to a stable random value
  so issued tokens survive restarts and the user is not forced to re-authorize
  on every deploy.

In production these come from repository Actions secrets, injected by the deploy
workflow into a gitignored `.env` on the VPS. The `/health` route stays public
regardless, for the container healthcheck.

### GitHub OAuth app setup (one time)

Create a GitHub OAuth App (Settings -> Developer settings -> OAuth Apps) with:

- Homepage URL: `https://wiki.florent-lejoly.be`
- Authorization callback URL: `https://wiki.florent-lejoly.be/auth/callback`

Then set the repository Actions secrets `GH_OAUTH_CLIENT_ID`,
`GH_OAUTH_CLIENT_SECRET`, `WIKI_JWT_SIGNING_KEY` (names must not start with
`GITHUB_`, which GitHub reserves).

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
3. URL: `https://wiki.florent-lejoly.be/mcp`.
4. Claude redirects you to GitHub to log in and consent. Only the allow-listed
   GitHub account can use the tools.
5. Ask Claude to call `ping`, then `read_long_term_index()`.

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
