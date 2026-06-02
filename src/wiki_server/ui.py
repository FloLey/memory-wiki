"""Web console for the Personal Memory Wiki, served at /ui by the same process.

A private, browser-facing console to consult and hand-edit the memory. It is
gated by GitHub login (the same OAuth app as the MCP endpoint) restricted to the
owner, with a signed session cookie. In local development (WIKI_AUTH_DISABLED=1)
the login is bypassed.

Rendering is server-side HTML. Markdown is rendered with HTML disabled, so
content stored in the wiki can never inject markup into the console.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from markdown_it import MarkdownIt
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import delete_file, write_file

SESSION_COOKIE = "wiki_ui_session"
STATE_COOKIE = "wiki_ui_state"
SESSION_TTL = 7 * 24 * 3600

_md = MarkdownIt("commonmark").enable("table")


# ----------------------------------------------------------------------------
# Signing helpers (stdlib HMAC; no extra dependency)
# ----------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: str, payload: dict) -> str:
    raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def _verify(secret: str, token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, sig = token.split(".", 1)
    expected = _b64e(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(raw))
        if not isinstance(payload, dict):
            return None
        exp = float(payload.get("exp", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if exp < time.time():
        return None
    return payload


# ----------------------------------------------------------------------------
# HTML templating (minimal, server-side)
# ----------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto;
       padding: 1.5rem; line-height: 1.5; }
header { display: flex; justify-content: space-between; align-items: baseline;
         border-bottom: 1px solid #8884; padding-bottom: .5rem; margin-bottom: 1rem; }
header a { margin-left: 1rem; }
nav a { margin-right: 1rem; }
article { border: 1px solid #8884; border-radius: 8px; padding: 1rem; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #8884; padding: .35rem .6rem; text-align: left; }
textarea { width: 100%; min-height: 60vh; font-family: ui-monospace, monospace;
           font-size: .9rem; box-sizing: border-box; }
input[type=text] { width: 100%; font-family: ui-monospace, monospace; box-sizing: border-box; }
.btn { display: inline-block; padding: .4rem .8rem; border: 1px solid #8888;
       border-radius: 6px; text-decoration: none; cursor: pointer; background: #8881; }
.danger { color: #c0392b; border-color: #c0392b; }
ul.tree { list-style: none; padding-left: 0; font-family: ui-monospace, monospace; }
ul.tree li { padding: .1rem 0; }
.muted { opacity: .6; font-size: .85rem; }
"""


def _page(title: str, body: str, *, login: str | None = None) -> HTMLResponse:
    nav = ""
    head_right = ""
    if login:
        nav = (
            '<nav><a href="/ui">Overview</a>'
            '<a href="/ui/edit">New page</a></nav>'
        )
        head_right = (
            f'<span class="muted">{html.escape(login)}</span>'
            '<a class="btn" href="/ui/logout">Logout</a>'
        )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} - Memory Wiki</title><style>{_CSS}</style></head>"
        f"<body><header><strong>Memory Wiki</strong><span>{head_right}</span></header>"
        f"{nav}<h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc)


# ----------------------------------------------------------------------------
# Wiki file helpers
# ----------------------------------------------------------------------------

def _list_md_files() -> list[str]:
    root = wiki_root()
    out = []
    for p in sorted(root.rglob("*.md")):
        parts = p.relative_to(root).parts
        if ".git" in parts or ("long_term" in parts and "private" in parts):
            continue
        out.append(p.relative_to(root).as_posix())
    return out


def register_ui(
    mcp,
    *,
    owner_login: str,
    client_id: str,
    client_secret: str,
    public_url: str,
    secret_key: str,
    auth_disabled: bool,
) -> None:
    """Register all /ui routes on the FastMCP app."""
    if not auth_disabled and (not secret_key or secret_key == "dev-insecure-key"):
        raise RuntimeError(
            "WIKI_JWT_SIGNING_KEY must be set to a secure, private value in "
            "production; it signs the web console session cookies."
        )
    owner = owner_login.strip().lower()
    base = public_url.rstrip("/")
    callback_url = f"{base}/ui/auth/callback"

    # ---- auth helpers ----
    def current_login(request: Request) -> str | None:
        if auth_disabled:
            return "dev"
        payload = _verify(secret_key, request.cookies.get(SESSION_COOKIE))
        return payload.get("login") if payload else None

    def csrf_token(login: str) -> str:
        return _sign(secret_key, {"k": "csrf", "login": login, "exp": time.time() + SESSION_TTL})

    def csrf_ok(login: str, token: str | None) -> bool:
        payload = _verify(secret_key, token)
        return bool(payload and payload.get("k") == "csrf" and payload.get("login") == login)

    # ---- login flow ----
    @mcp.custom_route("/ui/login", methods=["GET"])
    async def ui_login(request: Request) -> Response:
        if auth_disabled or current_login(request):
            return RedirectResponse("/ui")
        state = _sign(secret_key, {"k": "state", "n": secrets.token_urlsafe(8), "exp": time.time() + 600})
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": callback_url,
            "scope": "read:user",
            "state": state,
        })
        resp = RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="lax", path="/ui")
        return resp

    @mcp.custom_route("/ui/auth/callback", methods=["GET"])
    async def ui_callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        state_payload = _verify(secret_key, state) if state else None
        if (
            not code
            or not state
            or state != request.cookies.get(STATE_COOKIE)
            or not state_payload
            or state_payload.get("k") != "state"
        ):
            return PlainTextResponse("Invalid OAuth state.", status_code=400)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                tok = await client.post(
                    "https://github.com/login/oauth/access_token",
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": callback_url,
                    },
                )
                tok.raise_for_status()
                access = (tok.json() or {}).get("access_token")
                if not access:
                    return PlainTextResponse("OAuth exchange failed.", status_code=400)
                user = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"},
                )
                user.raise_for_status()
                github_login = (user.json() or {}).get("login", "")
        except (httpx.HTTPError, ValueError) as exc:
            return PlainTextResponse(f"GitHub authentication failed: {exc}", status_code=502)
        if github_login.strip().lower() != owner:
            return PlainTextResponse("Access denied: this console is private.", status_code=403)
        session = _sign(secret_key, {"login": github_login, "exp": time.time() + SESSION_TTL})
        resp = RedirectResponse("/ui", status_code=303)
        resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/ui")
        resp.delete_cookie(STATE_COOKIE, path="/ui")
        return resp

    @mcp.custom_route("/ui/logout", methods=["GET"])
    async def ui_logout(request: Request) -> Response:
        resp = RedirectResponse("/ui/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/ui")
        return resp

    # ---- consultation ----
    @mcp.custom_route("/ui", methods=["GET"])
    async def ui_overview(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        files = _list_md_files()
        items = "".join(
            f'<li><a href="/ui/page/{quote(f)}">{html.escape(f)}</a> '
            f'<a class="muted" href="/ui/edit?path={quote(f)}">edit</a></li>'
            for f in files
        )
        tree = f'<ul class="tree">{items}</ul>' if files else "<p class='muted'>No pages yet.</p>"
        body = f"<p class='muted'>{len(files)} markdown file(s) under the wiki root.</p>{tree}"
        return _page("Overview", body, login=login)

    @mcp.custom_route("/ui/page/{path:path}", methods=["GET"])
    async def ui_view(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        rel = request.path_params["path"]
        try:
            path = resolve_under_root(rel)
        except WikiPathError:
            return PlainTextResponse("Forbidden.", status_code=403)
        if not path.is_file():
            return PlainTextResponse("Not found.", status_code=404)
        rendered = _md.render(path.read_text(encoding="utf-8"))
        body = (
            f"<p><a class='btn' href='/ui/edit?path={quote(rel)}'>Edit</a></p>"
            f"<article>{rendered}</article>"
        )
        return _page(rel, body, login=login)

    # ---- editing ----
    @mcp.custom_route("/ui/edit", methods=["GET"])
    async def ui_edit_form(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        rel = request.query_params.get("path", "").strip()
        content = ""
        is_new = not rel
        if rel:
            try:
                path = resolve_under_root(rel)
            except WikiPathError:
                return PlainTextResponse("Forbidden.", status_code=403)
            if path.is_file():
                content = path.read_text(encoding="utf-8")
        token = csrf_token(login)
        path_attr = 'placeholder="long_term/self/identity.md"' if is_new else "readonly"
        path_field = (
            f'<input type="text" name="path" value="{html.escape(rel)}" {path_attr}>'
        )
        delete_btn = ""
        if rel and not is_new:
            delete_btn = (
                f'<form method="post" action="/ui/delete" style="display:inline" '
                f'onsubmit="return confirm(\'Delete {html.escape(rel)}?\')">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="path" value="{html.escape(rel)}">'
                f'<button class="btn danger" type="submit">Delete</button></form>'
            )
        body = (
            f'<form method="post" action="/ui/save">'
            f'<input type="hidden" name="csrf" value="{token}">'
            f'<p>Path: {path_field}</p>'
            f'<textarea name="content">{html.escape(content)}</textarea>'
            f'<p><button class="btn" type="submit">Save</button> {delete_btn}</p>'
            f'</form>'
        )
        return _page("New page" if is_new else f"Edit: {rel}", body, login=login)

    @mcp.custom_route("/ui/save", methods=["POST"])
    async def ui_save(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return PlainTextResponse("Unauthorized.", status_code=401)
        form = await request.form()
        if not csrf_ok(login, form.get("csrf")):
            return PlainTextResponse("Bad CSRF token.", status_code=403)
        rel = (form.get("path") or "").strip()
        content = form.get("content") or ""
        if not rel.endswith(".md"):
            return PlainTextResponse("Path must be a .md file.", status_code=400)
        try:
            write_file(rel, content, f"manual: edit {rel}")
        except WikiPathError:
            return PlainTextResponse("Forbidden.", status_code=403)
        return RedirectResponse(f"/ui/page/{quote(rel)}", status_code=303)

    @mcp.custom_route("/ui/delete", methods=["POST"])
    async def ui_delete(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return PlainTextResponse("Unauthorized.", status_code=401)
        form = await request.form()
        if not csrf_ok(login, form.get("csrf")):
            return PlainTextResponse("Bad CSRF token.", status_code=403)
        rel = (form.get("path") or "").strip()
        try:
            delete_file(rel, f"manual: delete {rel}")
        except WikiPathError:
            return PlainTextResponse("Forbidden.", status_code=403)
        return RedirectResponse("/ui", status_code=303)
