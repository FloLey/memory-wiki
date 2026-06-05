"""Web console for the Personal Memory Wiki, served at /ui by the same process.

A private, browser-facing console to consult and hand-edit the memory. It is
gated by GitHub login (the same OAuth app as the MCP endpoint) restricted to the
owner, with a signed session cookie. In local development (WIKI_AUTH_DISABLED=1)
the login is bypassed.

Rendering is server-side HTML. Markdown is rendered with HTML disabled, so
content stored in the wiki can never inject markup into the console.
"""

from __future__ import annotations

import html
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from markdown_it import MarkdownIt
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from wiki_server.paths import WikiPathError, resolve_under_root, wiki_root
from wiki_server.store import delete_file, write_file
from wiki_server.ui_assets import _page, _sign, _verify

SESSION_COOKIE = "wiki_ui_session"
STATE_COOKIE = "wiki_ui_state"
SESSION_TTL = 7 * 24 * 3600

_ROOT_GROUP = "_root"
_CATEGORY_LABELS = {
    "long_term": "Long-term memory",
    "short_term": "Short-term memory",
    _ROOT_GROUP: "Other",
}

_md = MarkdownIt("commonmark").enable("table")


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

    async def require_post(request: Request):
        """Gate a POST handler: returns (login, form, None) when authenticated with
        a valid CSRF token, or (None, None, error_response) to return immediately."""
        login = current_login(request)
        if not login:
            return None, None, PlainTextResponse("Unauthorized.", status_code=401)
        form = await request.form()
        csrf_val = form.get("csrf")
        if not isinstance(csrf_val, str) or not csrf_ok(login, csrf_val):
            return None, None, PlainTextResponse("Bad CSRF token.", status_code=403)
        return login, form, None

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
        resp.delete_cookie(STATE_COOKIE, path="/ui", secure=True, httponly=True, samesite="lax")
        return resp

    @mcp.custom_route("/ui/logout", methods=["GET"])
    async def ui_logout(request: Request) -> Response:
        # Land on a standalone page rather than /ui/login: redirecting into the
        # login flow would silently re-authenticate via the still-active GitHub
        # session, making logout look like a no-op.
        page = _page(
            "Logged out",
            "<div class='login-wrap'><div class='card'>"
            "<p>You are logged out.</p>"
            "<p><a class='btn' href='/ui/login'>Log in again</a></p>"
            "</div></div>",
        )
        page.delete_cookie(SESSION_COOKIE, path="/ui", secure=True, httponly=True, samesite="lax")
        return page

    # ---- consultation ----
    @mcp.custom_route("/ui", methods=["GET"])
    async def ui_overview(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        # The overview shows the memory itself. Machinery (policy, prompts, dream
        # reports) has its own tabs (Prompts, Dreams), so hide it here.
        hidden_top = {"dream_reports", "prompts"}
        hidden_files = {"DREAM.md"}
        files = [
            f for f in _list_md_files()
            if f.split("/", 1)[0] not in hidden_top and f not in hidden_files
        ]
        if not files:
            body = "<div class='empty'>No pages yet. Use <strong>New page</strong> to add one.</div>"
            return _page("Overview", body, login=login)
        groups: dict[str, list[str]] = {}
        for f in files:
            top = f.split("/", 1)[0] if "/" in f else _ROOT_GROUP
            groups.setdefault(top, []).append(f)
        cards = ""
        for top in sorted(groups, key=lambda k: (k == _ROOT_GROUP, k)):
            rows = "".join(
                f'<li><a class="name" href="/ui/page/{quote(f)}">{html.escape(f)}</a>'
                f'<a class="edit" href="/ui/edit?path={quote(f)}">edit</a></li>'
                for f in groups[top]
            )
            label = _CATEGORY_LABELS.get(top, top)
            cards += (
                f'<section class="card"><h2>{html.escape(label)} '
                f'<span class="muted">({len(groups[top])})</span></h2>'
                f'<ul class="filelist">{rows}</ul></section>'
            )
        body = f"<p class='muted'>{len(files)} page(s) across your memory.</p>{cards}"
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
        raw = path.read_text(encoding="utf-8")
        rendered = _md.render(raw)
        name = rel.rsplit("/", 1)[-1]
        crumb = rel[: -len(name) - 1] if "/" in rel else ""
        copy_btn = (
            '<button class="btn ghost" type="button" '
            "onclick=\"navigator.clipboard.writeText(document.getElementById('raw-md').value)"
            ".then(() => this.textContent = 'Copied')\">Copy markdown</button>"
        )
        body = (
            f"<div class='toolbar'><a class='btn' href='/ui/edit?path={quote(rel)}'>Edit</a>"
            f"<a class='btn ghost' href='/ui'>Back</a>{copy_btn}</div>"
            f"<article>{rendered}</article>"
            f'<textarea id="raw-md" hidden>{html.escape(raw)}</textarea>'
        )
        return _page(name, body, login=login, crumb=crumb)

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
        # Delete lives visually in the actions row but submits a sibling form via
        # the HTML5 form= attribute, so it is not nested inside the save form
        # (nested forms are invalid and the browser would route Delete to save).
        delete_button = ""
        delete_form = ""
        if rel and not is_new:
            delete_button = (
                '<button class="btn danger" type="submit" form="delete-form">Delete</button>'
            )
            delete_form = (
                f'<form id="delete-form" method="post" action="/ui/delete" '
                f'onsubmit="return confirm(\'Delete {html.escape(rel)}?\')">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="path" value="{html.escape(rel)}">'
                f'</form>'
            )
        cancel = f"/ui/page/{quote(rel)}" if rel and not is_new else "/ui"
        body = (
            f'<form method="post" action="/ui/save">'
            f'<input type="hidden" name="csrf" value="{token}">'
            f'<div class="field"><label>Path</label>{path_field}</div>'
            f'<div class="field"><label>Content (markdown)</label>'
            f'<textarea name="content">{html.escape(content)}</textarea></div>'
            f'<div class="actions"><button class="btn" type="submit">Save</button>'
            f'<a class="btn ghost" href="{cancel}">Cancel</a>{delete_button}</div>'
            f'</form>'
            f'{delete_form}'
        )
        name = rel.rsplit("/", 1)[-1] if rel else ""
        crumb = rel[: -len(name) - 1] if rel and "/" in rel else ""
        title = "New page" if is_new else f"Edit {name}"
        return _page(title, body, login=login, crumb=crumb)

    @mcp.custom_route("/ui/save", methods=["POST"])
    async def ui_save(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
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
        login, form, err = await require_post(request)
        if err:
            return err
        rel = (form.get("path") or "").strip()
        try:
            delete_file(rel, f"manual: delete {rel}")
        except WikiPathError:
            return PlainTextResponse("Forbidden.", status_code=403)
        return RedirectResponse("/ui", status_code=303)

    # ---- dreams (consolidation dry-run) ----
    @mcp.custom_route("/ui/dream", methods=["GET"])
    async def ui_dream(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        from wiki_server.dream import MODES, list_reports, read_schedule, usage_summary

        reports = list_reports()
        rows = "".join(
            f'<li><a class="name" href="/ui/page/{quote(r)}">{html.escape(r.rsplit("/", 1)[-1])}</a></li>'
            for r in reports
        )
        reports_html = (
            f'<section class="card"><h2>Reports</h2><ul class="filelist">{rows}</ul></section>'
            if reports
            else "<p class='muted'>No dream reports yet.</p>"
        )
        u = usage_summary()
        token = csrf_token(login)
        reset_cost_form = (
            '<form method="post" action="/ui/dream/reset-cost" '
            "onsubmit=\"return confirm('Reset the cost counter to zero?')\">"
            f'<input type="hidden" name="csrf" value="{token}">'
            '<button class="btn ghost" type="submit">Reset cost</button>'
            '</form>'
        )
        stages = u.get("by_stage") or {}
        stage_order = [s for s in ("triage", "decide", "write") if s in stages]
        stage_order += [s for s in sorted(stages) if s not in ("triage", "decide", "write")]
        stage_rows = "".join(
            f'<tr><th>{html.escape(s)}</th>'
            f'<td>{html.escape(", ".join(sorted(stages[s].get("models", []))) or "?")} : '
            f'${stages[s]["cost"]:.4f} '
            f'({stages[s]["input_tokens"]:,} in / {stages[s]["output_tokens"]:,} out)</td></tr>'
            for s in stage_order
        )
        stage_table = (
            f'<table><tbody>{stage_rows}</tbody></table>' if stage_rows
            else "<p class='muted'>Pas encore de detail par etape.</p>"
        )
        cost_html = (
            '<section class="card"><h2>Cost (estimated)</h2>'
            '<table><tbody>'
            f'<tr><th>Total</th><td>${u["total_cost"]:.4f} over {u["runs"]} run(s)</td></tr>'
            f'<tr><th>Per night (last)</th><td>${u["last_cost"]:.4f}</td></tr>'
            f'<tr><th>Per night (avg)</th><td>${u["avg_cost"]:.4f}</td></tr>'
            f'<tr><th>Tokens</th><td>{u["input_tokens"]:,} in / {u["output_tokens"]:,} out</td></tr>'
            '</tbody></table>'
            '<h3>Par etape (cumule)</h3>'
            f'{stage_table}'
            '<p class="muted">Token counts are exact; cost is estimated from '
            'Anthropic list prices for the model (per 1M tokens).</p>'
            f'{reset_cost_form}'
            '</section>'
        )
        dry_form = (
            '<form method="post" action="/ui/dream/run">'
            f'<input type="hidden" name="csrf" value="{token}">'
            '<button class="btn ghost" type="submit">Run a dream (dry-run)</button>'
            '</form>'
        )
        exec_form = (
            '<form method="post" action="/ui/dream/execute" '
            "onsubmit=\"return confirm('Apply the dream? It will modify your memory (one revertible commit).')\">"
            f'<input type="hidden" name="csrf" value="{token}">'
            '<button class="btn" type="submit">Execute a dream (apply)</button>'
            '</form>'
        )
        sched = read_schedule()
        mode_opts = "".join(
            f'<option value="{m}"{" selected" if m == sched["mode"] else ""}>{m}</option>'
            for m in MODES
        )
        schedule_card = (
            '<section class="card"><h2>Nightly dream (automatique)</h2>'
            "<p class='muted'>off : rien. dry-run : propose chaque nuit (tu valides au matin). "
            "execute : applique chaque nuit. Une seule fois par jour, apres l'heure choisie "
            f"({html.escape(sched['tz'])}), et seulement si la memoire court terme a assez "
            "d'entrees.</p>"
            '<form method="post" action="/ui/dream/schedule">'
            f'<input type="hidden" name="csrf" value="{token}">'
            f'<div class="field"><label for="s-mode">Mode</label>'
            f'<select id="s-mode" name="mode">{mode_opts}</select></div>'
            f'<div class="field"><label for="s-hour">Heure locale (0-23)</label>'
            f'<input type="number" id="s-hour" name="hour" min="0" max="23" value="{sched["hour"]}"></div>'
            f'<div class="field"><label for="s-min">Minimum d\'entrees court terme</label>'
            f'<input type="number" id="s-min" name="min_entries" min="1" value="{sched["min_entries"]}"></div>'
            '<button class="btn" type="submit">Enregistrer la planification</button>'
            '</form></section>'
        )
        body = (
            "<p class='muted'>A dry-run proposes a consolidation and changes nothing. "
            "Execute applies it: files short-term memory into long-term pages and "
            "temporal items, in one revertible commit. Nothing is ever deleted.</p>"
            f"<div class='toolbar'>{dry_form}{exec_form}</div>"
            f"{schedule_card}{cost_html}{reports_html}"
        )
        return _page("Dreams", body, login=login)

    @mcp.custom_route("/ui/dream/run", methods=["POST"])
    async def ui_dream_run(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from starlette.concurrency import run_in_threadpool
        from wiki_server.dream import run_dry_run

        rel, _ = await run_in_threadpool(run_dry_run)
        return RedirectResponse(f"/ui/page/{quote(rel)}", status_code=303)

    @mcp.custom_route("/ui/dream/execute", methods=["POST"])
    async def ui_dream_execute(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from starlette.concurrency import run_in_threadpool
        from wiki_server.dream import run_execute

        rel, _ = await run_in_threadpool(run_execute)
        return RedirectResponse(f"/ui/page/{quote(rel)}", status_code=303)

    @mcp.custom_route("/ui/dream/reset-cost", methods=["POST"])
    async def ui_dream_reset_cost(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from wiki_server.dream import reset_usage

        reset_usage()
        return RedirectResponse("/ui/dream", status_code=303)

    @mcp.custom_route("/ui/dream/schedule", methods=["POST"])
    async def ui_dream_schedule(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from wiki_server.dream import set_schedule

        set_schedule(form.get("mode"), form.get("hour"), min_entries=form.get("min_entries"))
        return RedirectResponse("/ui/dream", status_code=303)

    # ---- prompts (editable policy + stage prompts) ----
    @mcp.custom_route("/ui/prompts", methods=["GET"])
    async def ui_prompts(request: Request) -> Response:
        login = current_login(request)
        if not login:
            return RedirectResponse("/ui/login")
        from wiki_server.dream import (
            AVAILABLE_MODELS, DREAM_POLICY, STAGES, effective_models, ensure_policy,
        )
        from wiki_server.prompts import PROMPT_FILES, ensure_prompt

        # Seed defaults if absent, so they are always viewable/editable.
        ensure_policy()
        for stage in PROMPT_FILES:
            ensure_prompt(stage)

        rows = []
        items = [("DREAM.md", DREAM_POLICY, "Politique editoriale: quoi faire.")]
        items += [
            (f"prompts/{stage}", PROMPT_FILES[stage], desc)
            for stage, desc in (
                ("triage", "Etape 1: regrouper et router."),
                ("decide", "Etape 2: decider l'action par unite."),
                ("write", "Etape 3: rediger le contenu d'une page."),
            )
        ]
        for label, rel, desc in items:
            rows.append(
                f'<li><a class="name" href="/ui/page/{quote(rel)}">{html.escape(label)}</a>'
                f'<a class="edit" href="/ui/edit?path={quote(rel)}">edit</a>'
                f'<div class="muted">{html.escape(desc)}</div></li>'
            )
        # Per-stage model picker. Cheaper models on triage/decide cut most of the
        # nightly cost; write benefits most from a stronger model.
        effective = effective_models()
        stage_help = {
            "triage": "Etape 1 (1 appel par nuit).",
            "decide": "Etape 2 (1 appel par unite).",
            "write": "Etape 3 (1 appel par page ecrite).",
        }
        selects = []
        for stage in STAGES:
            current = effective.get(stage, "")
            options = []
            known = any(mid == current for mid, _ in AVAILABLE_MODELS)
            if current and not known:
                options.append(
                    f'<option value="{html.escape(current)}" selected>'
                    f'{html.escape(current)} (actuel)</option>'
                )
            for mid, label in AVAILABLE_MODELS:
                sel = " selected" if mid == current else ""
                options.append(f'<option value="{html.escape(mid)}"{sel}>{html.escape(label)}</option>')
            selects.append(
                f'<div class="field"><label for="m-{stage}"><strong>{stage}</strong> '
                f'<span class="muted">{stage_help[stage]}</span></label>'
                f'<select id="m-{stage}" name="{stage}">{"".join(options)}</select></div>'
            )
        models_form = (
            '<section class="card"><h2>Modeles par etape</h2>'
            "<p class='muted'>Le modele choisi ici l'emporte sur les variables "
            "d'environnement. Passer triage et decide sur un modele moins cher reduit "
            "fortement le cout par nuit.</p>"
            '<form method="post" action="/ui/prompts/models">'
            f'<input type="hidden" name="csrf" value="{csrf_token(login)}">'
            f'{"".join(selects)}'
            '<button class="btn" type="submit">Enregistrer les modeles</button>'
            '</form></section>'
        )
        # One-shot migration, only offered while a legacy entities/ folder exists.
        migrate_card = ""
        try:
            ent = resolve_under_root("long_term/entities")
            has_entities = ent.is_dir() and any(ent.glob("*.md"))
        except WikiPathError:
            has_entities = False
        if has_entities:
            migrate_card = (
                '<section class="card"><h2>Migration : entities -&gt; people / places / organizations</h2>'
                "<p class='muted'>D'anciennes pages sont encore dans la categorie entities. "
                "Cette action les classe (personne / lieu / organisation), les deplace, "
                "reecrit les liens et regenere l'index, en un seul commit reversible.</p>"
                '<form method="post" action="/ui/prompts/migrate-entities" '
                "onsubmit=\"return confirm('Migrer les pages entities ? Un commit reversible.')\">"
                f'<input type="hidden" name="csrf" value="{csrf_token(login)}">'
                '<button class="btn" type="submit">Migrer entities</button>'
                '</form></section>'
            )
        body = (
            "<p class='muted'>La politique et les trois prompts du daemon sont editables. "
            "Le schema de sortie JSON est ajoute par le code, donc editer les consignes ne "
            "casse jamais le contrat machine.</p>"
            f"<section class='card'><ul class='filelist'>{''.join(rows)}</ul></section>"
            f"{models_form}{migrate_card}"
        )
        return _page("Prompts", body, login=login)

    @mcp.custom_route("/ui/prompts/migrate-entities", methods=["POST"])
    async def ui_prompts_migrate(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from starlette.concurrency import run_in_threadpool
        from wiki_server.dream import migrate_entities

        rel, _ = await run_in_threadpool(migrate_entities)
        return RedirectResponse(f"/ui/page/{quote(rel)}", status_code=303)

    @mcp.custom_route("/ui/prompts/models", methods=["POST"])
    async def ui_prompts_models(request: Request) -> Response:
        login, form, err = await require_post(request)
        if err:
            return err
        from wiki_server.dream import STAGES, set_models

        set_models({s: form.get(s) for s in STAGES})
        return RedirectResponse("/ui/prompts", status_code=303)
