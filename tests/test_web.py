import base64
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


class PageTests(unittest.TestCase):
    def test_file_is_self_contained(self):
        text = PAGE.read_text(encoding="utf-8")
        for bad in ("http://", "https://", "innerHTML"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
