import hashlib
import http.client
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from tests.test_setup_page import SetupListener
from tfs import config
from tfs.cli import main, start
from tfs.engine import Engine
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.web.pairing import make_pairing_link


class PhonePairingTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.enterContext(mock.patch.dict("os.environ", {"TFS_HOME": folder.name}))
        self.enterContext(mock.patch("tfs.engine.HOME_WIFI_HOST", "127.0.0.1"))
        dashboard = self.home / "dashboard"
        (dashboard / "assets").mkdir(parents=True)
        (dashboard / "index.html").write_text("Invented dashboard")
        (dashboard / "assets" / "app.js").write_text("Invented asset")
        self.enterContext(mock.patch("tfs.web.server.DASHBOARD_DIR", dashboard))
        with closing(connect(config.db_path())) as conn:
            migrate(conn)

    def setup_server(self):
        server = SetupListener("127.0.0.1", 0, "example", "Example", [], lambda: None)
        server.start()
        self.addCleanup(server.stop)
        return server

    def engine_port(self):
        engine = Engine(self.home, home_wifi=True, home_wifi_port=0, screen_port=0)
        engine.start()
        self.addCleanup(engine.stop)
        return engine._running[1].sock.getsockname()[1]

    def request(self, port, path, cookie="", method="GET"):
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as client:
            client.request(method, path, headers={"Cookie": cookie})
            response = client.getresponse()
            return response.status, response.headers, response.read()

    def test_unpaired_visitors_only_see_the_scan_page(self):
        # GIVEN both Wi-Fi listeners and missing or incorrect phone tokens
        for port in (self.setup_server().port, self.engine_port()):
            for cookie in ("", "tfs_phone=invented"):
                for path in ("/", "/api/overview", "/assets/app.js"):
                    # WHEN the visitor requests private content
                    status, headers, body = self.request(port, path, cookie)
                    # THEN all three routes return the same public instruction
                    self.assertEqual(status, 404)
                    self.assertIn(b"Scan the QR code", body)
                    self.assertNotIn(b"Invented", body)
                    self.assertNotIn("Set-Cookie", headers)

    def test_setup_pairing_survives_start_until_the_owner_forgets_phones(self):
        # GIVEN a link made during setup
        server = self.setup_server()
        path = urlsplit(make_pairing_link(port=server.port)).path
        # WHEN the phone opens it once
        status, headers, _ = self.request(server.port, path)
        self.assertEqual((status, headers["Location"]), (303, "/"))
        token = SimpleCookie(headers["Set-Cookie"])["tfs_phone"]
        self.assertTrue(token["httponly"])
        self.assertEqual(token["samesite"].lower(), "strict")
        self.assertGreater(int(token["max-age"]), 86400)
        cookie = f"{token.key}={token.value}"
        self.assertNotIn("Set-Cookie", self.request(server.port, path)[1])
        self.assertEqual(self.request(server.port, "/", cookie)[2], b"Invented dashboard")
        server.stop()
        # THEN start enables Wi-Fi without a push source and honours the same cookie
        self.assertTrue(start._home_wifi_enabled())
        port = self.engine_port()
        for route in ("/", "/assets/app.js", "/api/overview"):
            status, headers, _ = self.request(port, route, cookie)
            self.assertEqual(status, 200)
            self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.request(port, "/push/invented", cookie, "POST")[0], 401)
        next_path = urlsplit(make_pairing_link(port=port)).path
        self.assertEqual(self.request(port, next_path)[0], 303)
        self.assertEqual(main(["phones", "forget"]), 0)
        self.assertIn(b"Scan the QR code", self.request(port, "/", cookie)[2])

    def test_expired_link_does_not_set_a_cookie(self):
        # GIVEN a link issued more than ten minutes ago
        port = self.engine_port()
        with mock.patch("tfs.web.pairing.datetime") as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=UTC)
            path = urlsplit(make_pairing_link(port=port)).path
        # WHEN a phone opens the expired link
        status, headers, _ = self.request(port, path)
        # THEN access is refused
        self.assertEqual(status, 404)
        self.assertNotIn("Set-Cookie", headers)

    def test_only_the_pairing_code_on_screen_ends_the_wait(self):
        # GIVEN two fresh links and setup waiting for the second
        engine = Engine(self.home, home_wifi=True, home_wifi_port=0, screen_port=0)
        engine.start()
        self.addCleanup(engine.stop)
        port = engine._running[1].sock.getsockname()[1]
        first, shown = (urlsplit(make_pairing_link(port=port)).path for _ in range(2))
        reached = threading.Event()
        engine.home_wifi.pairing_scan = (shown.rsplit("/", 1)[1], reached)
        # WHEN another phone pairs before the displayed link is scanned
        self.assertEqual(self.request(port, first)[0], 303)
        self.assertFalse(reached.is_set())
        # THEN the displayed code still works and releases the wait
        self.assertEqual(self.request(port, shown)[0], 303)
        self.assertTrue(reached.is_set())

    def test_two_scans_issue_only_one_token_and_store_only_hashes(self):
        # GIVEN one fresh link stored as a hash
        port = self.engine_port()
        path = urlsplit(make_pairing_link(port=port)).path
        self.assertTrue(start._home_wifi_enabled())
        with closing(connect(config.db_path(), "read_only")) as conn:
            query = "SELECT secret_hash FROM phone_pairings"
            code = path.rsplit("/", 1)[1]
            self.assertTrue(
                conn.execute(query).fetchone()[0] == hashlib.sha256(code.encode()).digest()
            )
            # WHEN two phones open the link
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda _: self.request(port, path), range(2)))
            # THEN one cookie is issued and its hash replaces the code hash
            self.assertEqual(sorted(status for status, _, _ in replies), [303, 404])
            cookies = [
                headers["Set-Cookie"] for _, headers, _ in replies if "Set-Cookie" in headers
            ]
            self.assertEqual(len(cookies), 1)
            token = SimpleCookie(cookies[0])["tfs_phone"].value
            self.assertTrue(
                [row[0] for row in conn.execute(query)] == [hashlib.sha256(token.encode()).digest()]
            )
