"""Who may use the dashboard: whoever holds the token in runtime/dashboard.token.

It listens on 127.0.0.1 only, but any web page you open could still send requests there.
So: a Host check (a DNS-rebinding page arrives with its own host name), a session cookie
that is HttpOnly and SameSite=Strict, and a CSRF token on every POST.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import security
from ..config import runtime_dir

COOKIE = "devflow_dashboard"
OPEN_PATHS = ("/login", "/static/")


def token_file() -> Path:
    return runtime_dir() / "dashboard.token"


def load_token() -> str:
    """The token, made on first use and kept, so a restart doesn't log you out."""
    p = token_file()
    if p.is_file():
        tok = p.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p.write_text(tok, encoding="utf-8")
    security.restrict(p)
    return tok


def csrf_for(token: str) -> str:
    return hmac.new(token.encode(), b"devflow-dashboard-csrf", hashlib.sha256).hexdigest()


class Guard:
    """ASGI middleware: Host check for everything, the cookie for all but OPEN_PATHS, CSRF on POST."""

    def __init__(self, app: ASGIApp, token: str, port: int):
        self.app, self.token, self.csrf = app, token, csrf_for(token)
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req = Request(scope)
        refused = self.refuse(req)
        if refused is not None:
            await refused(scope, receive, send)
            return
        if req.method == "POST":
            body = await _read_body(receive)
            if not hmac.compare_digest(_sent_csrf(req, body), self.csrf):
                await PlainTextResponse("missing or wrong CSRF token; reload the page", 403)(scope, receive, send)
                return
            receive = _replay(body)
        await self.app(scope, receive, send)

    def refuse(self, req: Request) -> Response | None:
        if req.headers.get("host", "") not in self.hosts:
            return PlainTextResponse("unknown host", 400)
        origin = req.headers.get("origin")
        if req.method == "POST" and origin and origin not in self.origins:
            return PlainTextResponse("cross-origin request refused", 403)
        if req.url.path.startswith(OPEN_PATHS):
            return None
        if not hmac.compare_digest(req.cookies.get(COOKIE, ""), self.token):
            return PlainTextResponse(
                "Not logged in. Open the login link devflow printed when it started "
                "(or run: devflow dashboard --url).", 401)
        return None


MAX_BODY = 1_000_000


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return b""  # too big for any form here: fails the CSRF check
        chunks.append(chunk)
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


def _sent_csrf(req: Request, body: bytes) -> str:
    header = req.headers.get("x-csrf", "")
    if header:
        return header
    if req.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        values = parse_qs(body.decode("utf-8", "replace")).get("csrf") or [""]
        return values[0]
    return ""
