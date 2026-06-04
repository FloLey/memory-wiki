"""Static assets and HTML shell for the web console.

The signed-cookie helpers (stdlib HMAC, no extra dependency), the stylesheet, and
the page template. Kept apart from ui.py so that module is just the routes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import time

from starlette.responses import HTMLResponse


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
:root {
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1d1d1f; --muted: #6b6b70;
  --line: #e3e3e0; --accent: #3a6ea5; --accent-ink: #fff; --danger: #c0392b;
  --shadow: 0 1px 2px rgba(0,0,0,.05), 0 8px 24px rgba(0,0,0,.04);
  --radius: 12px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --panel: #1f2126; --ink: #e8e8ea; --muted: #9a9aa2;
    --line: #2c2f36; --accent: #6ea8e0; --accent-ink: #10131a; --danger: #e57373;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.25);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink); line-height: 1.6;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.topbar {
  position: sticky; top: 0; z-index: 10; backdrop-filter: blur(8px);
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  border-bottom: 1px solid var(--line);
}
.topbar .inner {
  max-width: 820px; margin: 0 auto; padding: .8rem 1.25rem;
  display: flex; align-items: center; justify-content: space-between;
  gap: .5rem 1rem; flex-wrap: wrap;
}
.brand { font-weight: 700; letter-spacing: -.01em; text-decoration: none; color: var(--ink); }
.brand span { color: var(--accent); }
.topbar nav a { color: var(--muted); text-decoration: none; margin-left: 1rem; font-size: .92rem; }
.topbar nav a:hover { color: var(--ink); }
.who { color: var(--muted); font-size: .85rem; margin-right: .25rem; }
main { max-width: 820px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
h1 { font-size: 1.5rem; letter-spacing: -.02em; margin: .2rem 0 1.25rem; }
h1 .crumb { color: var(--muted); font-weight: 500; }
a { color: var(--accent); }
.muted { color: var(--muted); font-size: .85rem; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 1rem 1.25rem; margin-bottom: 1.1rem;
  overflow-x: auto;
}
.card h2 {
  font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); margin: 0 0 .6rem; font-weight: 600;
}
.filelist { list-style: none; margin: 0; padding: 0; }
.filelist li {
  display: flex; align-items: center; justify-content: space-between;
  padding: .4rem 0; border-top: 1px solid var(--line); gap: .75rem;
}
.filelist li:first-child { border-top: 0; }
.filelist a.name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9rem;
  text-decoration: none; color: var(--ink); overflow-wrap: anywhere;
}
.filelist a.name:hover { color: var(--accent); }
.filelist a.edit { font-size: .8rem; color: var(--muted); text-decoration: none; flex-shrink: 0; }
.filelist a.edit:hover { color: var(--accent); }
.btn {
  display: inline-block; padding: .5rem .9rem; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink);
  text-decoration: none; font-size: .9rem; font-weight: 500;
}
.btn.ghost { background: transparent; color: var(--accent); }
.btn.danger { background: transparent; border-color: var(--danger); color: var(--danger); }
.btn:hover { filter: brightness(1.05); }
.toolbar { display: flex; gap: .6rem; margin-bottom: 1rem; flex-wrap: wrap; }
article {
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 1.25rem 1.5rem; overflow-x: auto;
}
article :first-child { margin-top: 0; }
article h1, article h2, article h3 { letter-spacing: -.01em; }
article code {
  background: color-mix(in srgb, var(--ink) 8%, transparent); padding: .1rem .35rem;
  border-radius: 5px; font-size: .88em;
}
article pre { background: color-mix(in srgb, var(--ink) 6%, transparent); padding: 1rem; border-radius: 8px; overflow-x: auto; }
article pre code { background: none; padding: 0; }
article table { border-collapse: collapse; width: 100%; font-size: .9rem; }
article th, article td { border: 1px solid var(--line); padding: .45rem .7rem; text-align: left; }
article th { background: color-mix(in srgb, var(--ink) 5%, transparent); }
article blockquote { border-left: 3px solid var(--line); margin: 1rem 0; padding-left: 1rem; color: var(--muted); }
form { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
       box-shadow: var(--shadow); padding: 1.25rem; }
label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: .3rem; }
input[type=text], textarea, select {
  width: 100%; padding: .6rem .7rem; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--ink); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .9rem;
}
textarea { min-height: 58vh; line-height: 1.5; resize: vertical; }
.field { margin-bottom: 1rem; }
.actions { display: flex; gap: .6rem; margin-top: 1rem; align-items: center; }
.empty { color: var(--muted); text-align: center; padding: 2rem 0; }
.login-wrap { max-width: 380px; margin: 12vh auto; text-align: center; }
.login-wrap .card { padding: 2rem 1.5rem; }

@media (max-width: 640px) {
  .topbar .inner { flex-direction: column; align-items: flex-start; gap: .35rem; }
  .topbar nav { margin-top: .1rem; }
  .topbar nav a { margin-left: 0; margin-right: 1rem; }
  .who { display: block; }
  main { padding: 1.25rem 1rem 3rem; }
  h1 { font-size: 1.3rem; }
  .card { padding: .9rem 1rem; }
  .filelist li { flex-wrap: wrap; gap: .2rem .6rem; }
  textarea { min-height: 50vh; }
  .toolbar { gap: .5rem; }
}
"""


def _page(title: str, body: str, *, login: str | None = None, crumb: str = "") -> HTMLResponse:
    nav = ""
    who = ""
    if login:
        nav = (
            '<nav><a href="/ui">Overview</a>'
            '<a href="/ui/dream">Dreams</a>'
            '<a href="/ui/prompts">Prompts</a>'
            '<a href="/ui/edit">New page</a>'
            '<a href="/ui/logout">Logout</a></nav>'
        )
        who = f'<span class="who">{html.escape(login)}</span>'
    heading = html.escape(title)
    if crumb:
        heading = f'<span class="crumb">{html.escape(crumb)} /</span> {html.escape(title)}'
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} - Memory Wiki</title><style>{_CSS}</style></head>"
        "<body><div class='topbar'><div class='inner'>"
        "<a class='brand' href='/ui'>Memory<span>Wiki</span></a>"
        f"<div>{who}{nav}</div></div></div>"
        f"<main><h1>{heading}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(doc)
