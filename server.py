"""AHEAD web server: the Streamlit app plus a few plain HTTP routes.

Run with:  streamlit run server.py     (Render's start command does the same)

Routes added next to Streamlit:
  GET  /healthz       database check for Render's health check and wake-up pings
  POST /auth/session  swaps a one-time sign-in code for the HttpOnly session cookie
  POST /auth/logout   ends the session in this browser and deletes the cookie

`streamlit run app.py` still works for quick local UI work, but sign-ins then don't survive a reload.
"""

import json
import logging
from urllib.parse import urlsplit

import streamlit as st
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ahead import accounts, db
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


async def auth_session(request):
    if not _same_site(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        code = json.loads(await request.body() or b"{}").get("code", "")
    except (ValueError, AttributeError):
        code = ""
    result = await run_in_threadpool(accounts.complete_handoff, str(code))
    if not result:
        return JSONResponse({"error": "invalid or expired"}, status_code=400)
    token, max_age = result
    response = Response(status_code=204)
    response.set_cookie(accounts.COOKIE_NAME, token, max_age=max_age, path="/", httponly=True,
                        secure=_https(request), samesite="strict")
    return response


async def auth_logout(request):
    if not _same_site(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    token = request.cookies.get(accounts.COOKIE_NAME)
    if token:
        await run_in_threadpool(accounts.end_session, None, token)
    response = Response(status_code=204)
    response.delete_cookie(accounts.COOKIE_NAME, path="/", httponly=True, secure=_https(request), samesite="strict")
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


def _startup():
    init_db()
    try:
        accounts.prune()
    except Exception:
        log.exception("start-up clean-up failed")


_startup()

app = st.App(
    "app.py",
    routes=[Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/auth/session", auth_session, methods=["POST"]),
            Route("/auth/logout", auth_logout, methods=["POST"])],
    middleware=[Middleware(SecurityHeaders)],
)
