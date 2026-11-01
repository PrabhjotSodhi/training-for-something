import hashlib
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tfs import config, password_store
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.web.push import make_push_handler
from tfs.web.server import make_server

TOKEN = "test-token-not-a-real-secret"
NOW = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
NOW_TEXT = "2026-10-27T07:00:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "health_auto_export"
# Client side only. A broken server fails a test instead of hanging it.
SAFETY_TIMEOUT_S = 10


class PushTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        env = mock.patch.dict(
            os.environ, {"TFS_HOME": str(self.data_dir), "TFS_PASSWORD_STORE": "file"}
        )
        env.start()
        self.addCleanup(env.stop)
        password_store.set("push_token", TOKEN)

        self.db_path = self.data_dir / "tfs.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        migrate(self.conn)

        self.saved: list[str] = []
        handler = make_push_handler(
            data_dir=self.data_dir,
            conn_factory=lambda: connect(self.db_path, "save"),
            now_fn=lambda: NOW,
            on_saved=self.saved.append,
        )
        server = make_server(config.SCREEN_HOST, 0, push_handler=handler, read_timeout_s=5)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.server = server
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def post(self, path: str, body: bytes, token: str | None = TOKEN) -> tuple[int, dict]:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        host, port = self.server.server_address
        client = http.client.HTTPConnection(host, port, timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(client.close)
        client.request("POST", path, body=body, headers=headers)
        response = client.getresponse()
        return response.status, json.loads(response.read())

    def raw_files(self) -> list[Path]:
        return [path for path in (self.data_dir / "raw").rglob("*") if path.is_file()]

    def file_rows(self) -> list:
        return self.conn.execute("SELECT * FROM files").fetchall()

    def assert_nothing_saved(self) -> None:
        self.assertEqual(self.raw_files(), [])
        self.assertEqual(self.file_rows(), [])
        self.assertEqual(self.saved, [])

    def test_right_token_saves_the_body_and_answers_with_its_hash(self):
        body = (FIXTURES / "metrics.json").read_bytes()

        status, reply = self.post("/push/health_auto_export", body)

        self.assertEqual(status, 200)
        sha256 = hashlib.sha256(body).hexdigest()
        self.assertEqual(reply, {"ok": True, "sha256": sha256})
        saved = self.data_dir / "raw" / "health_auto_export" / "rest_json" / f"{sha256}.json"
        self.assertEqual(saved.read_bytes(), body)
        (row,) = self.file_rows()
        self.assertEqual((row["sha256"], row["status"]), (sha256, "new"))
        source = self.conn.execute("SELECT * FROM sources").fetchone()
        self.assertEqual(source["last_contact_at"], NOW_TEXT)
        self.assertEqual(self.saved, [sha256])

    def test_wrong_or_missing_token_gets_401_and_saves_nothing(self):
        body = b'{"data": {}}'
        for token in ("wrong-token", "", None):
            with self.subTest(token=token):
                status, _ = self.post("/push/health_auto_export", body, token)
                self.assertEqual(status, 401)
        self.assert_nothing_saved()

    def test_a_connector_without_push_or_unknown_gets_404(self):
        for path in ("/push/hevy", "/push/unknown"):
            with self.subTest(path=path):
                status, _ = self.post(path, b'{"data": {}}')
                self.assertEqual(status, 404)
        self.assert_nothing_saved()

    def test_body_that_stops_early_saves_nothing(self):
        with socket.create_connection(self.server.server_address, timeout=SAFETY_TIMEOUT_S) as s:
            s.sendall(
                b"POST /push/health_auto_export HTTP/1.1\r\nHost: tfs.test\r\n"
                b"Authorization: Bearer " + TOKEN.encode() + b"\r\nContent-Length: 5\r\n\r\nab"
            )
            s.shutdown(socket.SHUT_WR)
            while s.recv(65536):
                pass
        self.assert_nothing_saved()

    def test_the_same_body_twice_answers_200_and_keeps_one_row(self):
        body = (FIXTURES / "metrics.json").read_bytes()

        first = self.post("/push/health_auto_export", body)
        second = self.post("/push/health_auto_export", body)

        self.assertEqual(first[0], 200)
        self.assertEqual(second, first)
        self.assertEqual(len(self.file_rows()), 1)


if __name__ == "__main__":
    unittest.main()
