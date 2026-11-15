"""Read-only web dashboard: one HTML page and one JSON endpoint, behind HTTP Basic auth."""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, stats
from .config import Config

log = logging.getLogger(__name__)
PAGE_PATH = Path(__file__).with_name("dashboard.html")
CHART_PATH = Path(__file__).with_name("static") / "chart.umd.min.js"
CHART_ROUTE = "/static/chart.umd.min.js"
REALM = "media-organizer"


class _Handler(BaseHTTPRequestHandler):
    server_version = "media-organizer"
    timeout = 15  # seconds; an idle client must not hold a thread forever

    def log_message(self, format, *args):  # never log headers, so never the password
        log.info("web %s %r", self.address_string(), self.requestline.split(" HTTP")[0])

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return False
        try:
            _, sep, password = base64.b64decode(token.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        expected = self.server.password.encode()
        return bool(sep) and hmac.compare_digest(password.encode(), expected)

    def _send(self, code: int, body: bytes, content_type: str, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not any(key == "Cache-Control" for key, _ in extra):
            self.send_header("Cache-Control", "no-store")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str, extra=()) -> None:
        self._send(code, text.encode(), "text/plain; charset=utf-8", extra)

    def do_GET(self):
        if not self._authorized():
            self._text(401, "authentication required",
                       [("WWW-Authenticate", f'Basic realm="{REALM}"')])
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path == CHART_ROUTE:
            self._send(200, self.server.chart_js, "application/javascript; charset=utf-8",
                       [("Cache-Control", "max-age=86400")])
        elif self.path == "/api/summary":
            con = db.open_db(self.server.cfg.db_path)
            try:
                body = json.dumps(stats.summary(con, self.server.cfg)).encode()
            finally:
                con.close()
            self._send(200, body, "application/json")
        else:
            self._text(404, "not found")

    def _not_allowed(self):
        self._text(405, "read-only", [("Allow", "GET")])

    do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, cfg: Config):
        chart_js = CHART_PATH.read_bytes()  # the one static file, read once, before binding
        super().__init__(address, _Handler)
        self.cfg = cfg
        self.password = cfg.web_password
        self.chart_js = chart_js

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            log.debug("client %s went away", client_address)
            return
        super().handle_error(request, client_address)


def make_server(cfg: Config, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    if not cfg.web_password:
        raise ValueError("web_password is empty; the dashboard will not start without one")
    return DashboardServer((host, cfg.web_port), cfg)


def serve_in_thread(cfg: Config) -> ThreadingHTTPServer | None:
    try:
        server = make_server(cfg)
    except ValueError as err:
        log.warning("dashboard disabled: %s", err)
        return None
    except OSError as err:
        log.warning("dashboard disabled: cannot bind port %s: %s", cfg.web_port, err)
        return None
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("dashboard on port %s", server.server_address[1])
    return server
