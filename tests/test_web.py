import base64
import contextlib
import io
import socket
import time
import json
import logging
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from media_organizer import db, web
from tests.helpers import test_config

PAGE = Path(web.__file__).with_name("dashboard.html")
STATIC = Path(web.__file__).with_name("static") / "chart.umd.min.js"
PASSWORD = "s3cret"


def auth(password, user="anyone"):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(test_config(Path(self.tmp.name)), web_password=PASSWORD, web_port=0)
        self.server = web.make_server(self.cfg, host="127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            self.addCleanup(err.close)
            return err.code, err.headers, err.read()


class AuthTests(WebCase):
    def test_no_credentials(self):
        status, headers, _ = self.get("/")
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], 'Basic realm="media-organizer"')

    def test_wrong_password(self):
        self.assertEqual(self.get("/", auth("nope"))[0], 401)
        self.assertEqual(self.get("/api/summary", auth("nope"))[0], 401)

    def test_malformed_header(self):
        for value in ("Basic !!!notbase64", "Basic", "Bearer abc", "Basic " + "QQ=="):
            self.assertEqual(self.get("/", {"Authorization": value})[0], 401, value)

    def test_non_ascii_password_is_401_not_crash(self):
        self.assertEqual(self.get("/", auth("pässword"))[0], 401)

    def test_unknown_path_needs_auth_first(self):
        self.assertEqual(self.get("/nope")[0], 401)


class RouteTests(WebCase):
    def test_dashboard(self):
        status, headers, body = self.get("/", auth(PASSWORD))
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"Media Organizer", body)
        self.assertEqual(body, PAGE.read_bytes())
        for bad in (b"http://", b"https://", b"innerHTML"):
            self.assertNotIn(bad, body)

    def test_dashboard_shows_waiting_release_and_stuck_counts(self):
        text = PAGE.read_text()
        for needle in ("q.no_space", "waiting for space", "q.evict_pending",
                       "to release on the Mac", "p.stuck_published", 'p["multi-frame"]'):
            self.assertIn(needle, text)

    def test_summary(self):
        status, headers, body = self.get("/api/summary", auth(PASSWORD))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("milestones", json.loads(body))

    def test_summary_reflects_database(self):
        con = db.open_db(self.cfg.db_path)
        db.meta_set(con, "paused", "1")
        con.close()
        data = json.loads(self.get("/api/summary", auth(PASSWORD))[2])
        self.assertTrue(data["paused"])

    def test_not_found(self):
        self.assertEqual(self.get("/nope", auth(PASSWORD))[0], 404)

    def test_other_methods(self):
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(self.base + "/", data=b"x" if method != "DELETE" else None,
                                         headers=auth(PASSWORD), method=method)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.addCleanup(ctx.exception.close)
            self.assertEqual(ctx.exception.code, 405, method)


class StaticTests(WebCase):
    def test_chart_library_needs_auth(self):
        self.assertEqual(self.get("/static/chart.umd.min.js")[0], 401)
        self.assertEqual(self.get("/static/chart.umd.min.js", auth("nope"))[0], 401)

    def test_chart_library_served_byte_identical(self):
        status, headers, body = self.get("/static/chart.umd.min.js", auth(PASSWORD))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")
        self.assertEqual(headers.get_all("Cache-Control"), ["max-age=86400"])
        self.assertEqual(body, STATIC.read_bytes())
        self.assertIn(b"Chart.js v4.4.1", body[:200])

    def test_no_other_static_paths(self):
        for path in ("/static/", "/static", "/static/other.js", "/static/chart.umd.min.js/",
                     "/static/chart.umd.min.js?x=1", "/static/../web.py", "/static/%2e%2e/web.py",
                     "/static/dashboard.html"):
            self.assertEqual(self.get(path, auth(PASSWORD))[0], 404, path)

    def test_page_references_library_before_its_script(self):
        text = PAGE.read_text(encoding="utf-8")
        ref = '<script src="static/chart.umd.min.js"></script>'
        self.assertIn(ref, text)
        self.assertLess(text.index(ref), text.index('<script>'))
        self.assertIn("Charts unavailable", text)
        self.assertNotIn("createElementNS", text)

    def test_package_data_includes_static_file(self):
        import tomllib
        root = Path(web.__file__).parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = data["tool"]["setuptools"]["package-data"]["media_organizer"]
        self.assertIn("static/*.js", patterns)
        self.assertTrue(STATIC.is_file())


class ServerSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_empty_password_refused(self):
        cfg = test_config(Path(self.tmp.name), web_port=0)
        with self.assertRaises(ValueError):
            web.make_server(cfg, host="127.0.0.1")

    def test_serve_in_thread_without_password(self):
        cfg = test_config(Path(self.tmp.name), web_port=0)
        with self.assertLogs("media_organizer.web", logging.WARNING):
            self.assertIsNone(web.serve_in_thread(cfg))

    def test_serve_in_thread_port_in_use(self):
        cfg = replace(test_config(Path(self.tmp.name)), web_password=PASSWORD, web_port=0)
        first = web.make_server(cfg)
        self.addCleanup(first.server_close)
        busy = replace(cfg, web_port=first.server_address[1])
        with self.assertLogs("media_organizer.web", logging.WARNING):
            self.assertIsNone(web.serve_in_thread(busy))

    def test_serve_in_thread_serves(self):
        cfg = replace(test_config(Path(self.tmp.name)), web_password=PASSWORD, web_port=0)
        server = web.serve_in_thread(cfg)
        self.assertIsNotNone(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/summary", headers=auth(PASSWORD))
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)


class HardeningTests(WebCase):
    def test_idle_connection_is_closed(self):
        web._Handler.timeout = 1
        self.addCleanup(setattr, web._Handler, "timeout", 15)
        self.assertEqual(web._Handler.__dict__.get("timeout"), 1)
        sock = socket.create_connection(("127.0.0.1", self.server.server_address[1]))
        self.addCleanup(sock.close)
        sock.settimeout(10)
        start = time.time()
        self.assertEqual(sock.recv(10), b"")
        self.assertLess(time.time() - start, 8)

    def test_handler_timeout_default(self):
        self.assertEqual(web._Handler.timeout, 15)

    def test_client_disconnect_is_quiet(self):
        err = io.StringIO()
        for exc in (BrokenPipeError(), ConnectionResetError()):
            try:
                raise exc
            except OSError:
                with contextlib.redirect_stderr(err):
                    self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")

    def test_other_errors_keep_default_traceback(self):
        err = io.StringIO()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            with contextlib.redirect_stderr(err):
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("boom", err.getvalue())


class PageTests(unittest.TestCase):
    def test_grids_cannot_overflow_viewport(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", text)
        self.assertIn(".tiles, .cols { grid-template-columns: minmax(0, 1fr); }", text)
        self.assertIn(".cols > div", text)
        self.assertIn("min-width: 0", text)
        self.assertNotIn("1fr 1fr", text)

    def test_file_is_self_contained(self):
        text = PAGE.read_text(encoding="utf-8")
        for bad in ("http://", "https://", "innerHTML"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
