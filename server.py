"""AHEAD web server: the Streamlit app plus a few plain HTTP routes.

Run with:  streamlit run server.py     (Render's start command does the same)

Routes added next to Streamlit:
  GET  /healthz       database check for Render's health check and wake-up pings
  POST /auth/session  swaps a one-time sign-in code for this tab's HttpOnly session cookie
  POST /auth/logout   ends this tab's session and deletes its cookie (other tabs keep theirs)

It also starts the background email sender (ahead/mailer.py) when BREVO_API_KEY is set, and loads the
models in the background so the first screening after a restart doesn't wait (AHEAD_WARM_UP=0 turns it off).

`streamlit run app.py` still works for quick local UI work, but sign-ins then don't survive a reload.
"""

import json
import logging
import os
import threading
from urllib.parse import urlsplit

import streamlit as st
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ahead import accounts, db, mailer
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


class SecurityHeaders:
    """Headers on every HTTP response. A full Content-Security-Policy is a later step (Streamlit needs testing)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        https = headers.get("x-forwarded-proto", scope.get("scheme", "http")).split(",")[0].strip() == "https"

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                         (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'"),
                         (b"referrer-policy", b"same-origin"),
                         (b"permissions-policy", b"camera=(), geolocation=(), payment=(), usb=()")]
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


def _startup():
    init_db()
    try:
        accounts.prune()
    except Exception:
        log.exception("start-up clean-up failed")
    mailer.start_background_sender()
    if os.environ.get("AHEAD_WARM_UP", "1") != "0":
        threading.Thread(target=_warm_up, name="ahead-warm-up", daemon=True).start()


_startup()

app = st.App(
    "app.py",
    routes=[Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/auth/session", auth_session, methods=["POST"]),
            Route("/auth/logout", auth_logout, methods=["POST"])],
    middleware=[Middleware(SecurityHeaders)],
)
