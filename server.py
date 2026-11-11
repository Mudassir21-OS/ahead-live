"""AHEAD web server: the Streamlit app plus a few plain HTTP routes.

Run with:  streamlit run server.py     (Render's start command does the same)

Routes added next to Streamlit:
  GET  /healthz       database check for Render's health check and wake-up pings
  POST /auth/session  swaps a one-time sign-in code for this tab's HttpOnly session cookie
  POST /auth/logout   ends this tab's session and deletes its cookie (other tabs keep theirs)
  GET  /calendar/<token>.ics  a patient's private medicine-reminder calendar (subscribe or download)
  POST /tasks/weekly  weekly summary emails; needs the X-AHEAD-Task-Key header (GitHub Actions calls it)

It also starts the background email sender (ahead/mailer.py) when BREVO_API_KEY is set, and loads the
models in the background so the first screening after a restart doesn't wait (AHEAD_WARM_UP=0 turns it off).

Phase 3b: start-up stops if the database holds encrypted health values that AHEAD_FIELD_KEY can't open
(ahead/fieldcrypt.py), then a background job encrypts anything still unencrypted and applies the retention
rules (ahead/retention.py). Every response carries a Content-Security-Policy (see SecurityHeaders).

`streamlit run app.py` still works for quick local UI work, but sign-ins then don't survive a reload.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ahead import accounts, alerts, calendar_feed, db, fieldcrypt, mailer, retention
from ahead.storage import init_db

log = logging.getLogger("ahead.server")


def _https(request):
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


def _same_site(request):
    """Reject cross-site POSTs: the Origin (or Referer) host must be this site's host."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = request.headers.get("host", "")
    return bool(origin) and urlsplit(origin).netloc == host


async def healthz(request):
    try:
        ok = await run_in_threadpool(db.ping)
    except Exception:
        log.exception("health check: database unavailable")
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "database": "ok" if ok else "unavailable",
                         "backend": db.describe_target().split("@")[0]}, status_code=200 if ok else 503)


async def _json_body(request):
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _delete(response, request, name):
    response.delete_cookie(name, path="/", httponly=True, secure=_https(request), samesite="strict")


def _dead_tab_cookies(cookies, keep):
    """Cookies of tabs whose sessions have ended (signed out, idle, closed long ago), to tidy them away."""
    return [name for name, token in cookies.items()
            if name.startswith(accounts.COOKIE_PREFIX) and name != keep and not accounts.token_is_live(token)]


async def auth_session(request):
    if not _same_site(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await _json_body(request)
    code, tab_id = str(body.get("code", "")), body.get("tab", "")
    if not accounts.valid_tab_id(tab_id):
        return JSONResponse({"error": "invalid tab"}, status_code=400)
    result = await run_in_threadpool(accounts.complete_handoff, code)
    if not result:
        return JSONResponse({"error": "invalid or expired"}, status_code=400)
    token, max_age = result
    name = accounts.cookie_name(tab_id)
    response = Response(status_code=204)
    response.set_cookie(name, token, max_age=max_age, path="/", httponly=True, secure=_https(request), samesite="strict")
    for dead in await run_in_threadpool(_dead_tab_cookies, dict(request.cookies), name):
        _delete(response, request, dead)
    if accounts.LEGACY_COOKIE in request.cookies:
        _delete(response, request, accounts.LEGACY_COOKIE)
    return response


async def auth_logout(request):
    if not _same_site(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    tab_id = (await _json_body(request)).get("tab", "")
    response = Response(status_code=204)
    if accounts.valid_tab_id(tab_id):
        name = accounts.cookie_name(tab_id)
        token = request.cookies.get(name)
        if token:
            await run_in_threadpool(accounts.end_session, None, token)
        _delete(response, request, name)
    if accounts.LEGACY_COOKIE in request.cookies:
        _delete(response, request, accounts.LEGACY_COOKIE)
    return response


async def calendar(request):
    """GET /calendar/<token>.ics: a patient's private medicine-reminder calendar (ahead/calendar_feed.py)."""
    token = request.path_params.get("token", "").removesuffix(".ics")
    body = await run_in_threadpool(calendar_feed.ics_for_token, token, accounts.site_url_for(request))
    if body is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"cache-control": "private, max-age=900", "content-disposition": 'inline; filename="ahead.ics"'})


async def weekly_task(request):
    """POST /tasks/weekly with header X-AHEAD-Task-Key: the weekly summary emails (a GitHub Actions schedule calls it,
    since Render's free plan has no cron). Without AHEAD_TASK_KEY set on the server the route doesn't exist."""
    key = os.environ.get("AHEAD_TASK_KEY", "").strip()
    if len(key) < 24:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not hmac.compare_digest(request.headers.get("x-ahead-task-key", ""), key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    result = await run_in_threadpool(alerts.weekly_summaries)
    result["maintenance"] = await run_in_threadpool(_maintenance, True)
    return JSONResponse(result)


def _index_script_hashes():
    """CSP hashes of the inline <script> blocks in Streamlit's own index.html (read from the installed package,
    so a Streamlit upgrade can't silently break the page)."""
    try:
        html = (Path(st.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    except OSError:
        log.exception("could not read Streamlit's index.html for the Content-Security-Policy")
        return []
    return [f"'sha256-{base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()}'"
            for body in re.findall(r"<script>(.*?)</script>", html, re.S)]


def content_security_policy(https=False, host=""):
    """What the browser may load and run on AHEAD's pages.

    * Scripts only from AHEAD itself: Streamlit's bundle, its one inline line (allowed by its hash) and AHEAD's
      small components (ahead/session.py, ahead/browser.py), which Streamlit loads as blob: modules. No inline
      scripts, no eval (WebAssembly compilation is allowed: Streamlit's table widget uses it), no other sites.
    * Styles may be inline (Streamlit and its charts write <style> elements and style attributes at run time),
      plus Google Fonts for the Inter typeface.
    * Images, fonts and media from AHEAD or data:/blob: URLs (charts, uploads, the voice recorder).
    * Network requests only to AHEAD, its websocket included: Gemini and Brevo are called by the server.
    * Never inside another site's frame; forms post only to AHEAD; <base> can't redirect links; no plugins.
    """
    policy = ["default-src 'self'",
              "script-src 'self' blob: 'wasm-unsafe-eval' " + " ".join(_SCRIPT_HASHES),
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              "img-src 'self' data: blob:",
              "font-src 'self' data: https://fonts.gstatic.com",
              "media-src 'self' data: blob:",
              # 'self' covers the websocket in current browsers; the explicit address helps older Safari.
              "connect-src 'self'" + (f" {'wss' if https else 'ws'}://{host}" if _HOST.fullmatch(host or "") else ""),
              "worker-src 'self' blob:",
              "manifest-src 'self'",
              "form-action 'self'",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "object-src 'none'"]
    if https:
        policy.append("upgrade-insecure-requests")
    return "; ".join(policy)


_SCRIPT_HASHES = _index_script_hashes()
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")


class SecurityHeaders:
    """Headers on every HTTP response, including the Content-Security-Policy above.

    AHEAD_CSP=report (Render → Environment) switches the policy to report-only, as a quick way out if a future
    Streamlit version needs something it doesn't allow: the browser console then lists what would be blocked."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        https = headers.get("x-forwarded-proto", scope.get("scheme", "http")).split(",")[0].strip() == "https"
        report_only = os.environ.get("AHEAD_CSP", "").strip().lower() == "report"
        csp_header = b"content-security-policy-report-only" if report_only else b"content-security-policy"
        csp = content_security_policy(https, headers.get("host", "")).encode()

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                         (csp_header, csp), (b"referrer-policy", b"same-origin"),
                         (b"cross-origin-opener-policy", b"same-origin"),
                         (b"permissions-policy", b"camera=(), geolocation=(), payment=(), usb=()")]
                if report_only:   # the parts that matter most stay enforced
                    extra.append((b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'"))
                if https:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _warm_up():
    """Background: wait for Streamlit's runtime (so the caches are the ones pages use), then load models."""
    import time
    from streamlit.runtime import Runtime
    from ahead.resources import warm_up
    for _ in range(600):
        if Runtime.exists():
            break
        time.sleep(0.1)
    # Outside a page run Streamlit warns "missing ScriptRunContext" on every cached call; it's harmless here.
    noisy = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
    level, started = noisy.level, time.monotonic()
    noisy.setLevel(logging.ERROR)
    try:
        warm_up()
        print(f"AHEAD: models warmed up in {time.monotonic() - started:.1f} s", flush=True)
    except Exception:
        log.exception("model warm-up failed (pages will load models when first needed)")
    finally:
        noisy.setLevel(level)


def _maintenance(weekly=False):
    """Encrypt values saved before the key was set (or under an old key), then apply the retention rules.
    Start-up runs it in the background; the weekly task runs it too. Returns counts (never any data)."""
    done = {}
    try:
        done["encrypted"] = fieldcrypt.encrypt_existing()
        if done["encrypted"]:
            print(f"AHEAD: encrypted {done['encrypted']} stored health values", flush=True)
    except Exception:
        log.exception("encrypting stored values failed (tried again next time)")
        done["encrypted"] = "error"
    try:
        done["retention"] = retention.run(force=weekly)
    except Exception:
        log.exception("retention clean-up failed (tried again next time)")
        done["retention"] = "error"
    return done


def _startup():
    init_db()
    try:
        fieldcrypt.check()
    except fieldcrypt.FieldKeyError as error:
        # Better no site than pages that can't read their data: Render keeps the previous deploy running.
        print(f"AHEAD can't start: {error}", flush=True)
        raise
    problem = accounts.public_url_problem()
    if problem:
        print(f"AHEAD can't start: {problem}", flush=True)
        raise SystemExit(1)
    try:
        accounts.prune()
    except Exception:
        log.exception("start-up clean-up failed")
    mailer.start_background_sender()
    if os.environ.get("AHEAD_WARM_UP", "1") != "0":
        threading.Thread(target=_warm_up, name="ahead-warm-up", daemon=True).start()
    if os.environ.get("AHEAD_STARTUP_MAINTENANCE", "1") != "0":
        threading.Thread(target=_delayed_maintenance, name="ahead-maintenance", daemon=True).start()


def _delayed_maintenance():
    """After a deploy the previous version keeps serving open tabs for a minute or two, and it can't read encrypted
    values: wait before encrypting older ones, so those tabs don't break."""
    import time
    time.sleep(float(os.environ.get("AHEAD_MAINTENANCE_DELAY", "180")))
    _maintenance()


_startup()

app = st.App(
    "app.py",
    routes=[Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/auth/session", auth_session, methods=["POST"]),
            Route("/auth/logout", auth_logout, methods=["POST"]),
            Route("/calendar/{token}", calendar, methods=["GET", "HEAD"]),
            Route("/tasks/weekly", weekly_task, methods=["POST"])],
    middleware=[Middleware(SecurityHeaders)],
)
