import contextlib
import hashlib
import http.client
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_engine import free_port
from tfs import config, password_store
from tfs.cli import main, start
from tfs.cli.start import render_banner
from tfs.connectors.core import runner
from tfs.engine import Engine
from tfs.importer.raw_store import save_raw
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

FIXTURES = Path(__file__).parent / "fixtures" / "health_auto_export"
NOW = "2026-10-27T07:00:00Z"
# Client side only. A broken server fails a test instead of hanging it.
SAFETY_TIMEOUT_S = 10
CONNECTORS = [("health_auto_export", "Health Auto Export")]
BANNER = """TFS is running. Press Ctrl+C to stop.
Health Auto Export
  Push URL (this computer): http://127.0.0.1:47823/push/health_auto_export
"""
HOME_WIFI_LINE = "  Push URL (home Wi-Fi):    http://ana-pc.local:47824/push/health_auto_export\n"
TOKEN_LINE = "Push token: tok   Keep this secret."


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class EngineWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        env = mock.patch.dict(
            os.environ, {"TFS_HOME": str(self.data_dir), "TFS_PASSWORD_STORE": "file"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.db_path = self.data_dir / "tfs.db"
        self.read_calls: list[str] = []

    def count_reads(self, *, fail_for: str | None = None) -> None:
        """Record the file of every read. A read of the file named fail_for fails."""
        real_run = runner.run

        def run(connector, op, payload, **options):
            sha256 = Path(payload["path"]).stem
            self.read_calls.append(sha256)
            if sha256 == fail_for:
                raise runner.RunnerFailedError("The reader crashed.")
            return real_run(connector, op, payload, **options)

        patch = mock.patch.object(runner, "run", run)
        patch.start()
        self.addCleanup(patch.stop)

    def start_engine(self) -> Engine:
        self.port = free_port()
        running = Engine(self.data_dir, screen_port=self.port)
        self.addCleanup(running.stop)
        running.start()
        return running

    def push(self, body: bytes) -> None:
        client = http.client.HTTPConnection(config.SCREEN_HOST, self.port, timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(client.close)
        token = password_store.get("push_token")
        client.request(
            "POST",
            "/push/health_auto_export",
            body=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        response.read()

    def file_statuses(self) -> dict[str, str]:
        conn = connect(self.db_path, "read_only")
        try:
            return dict(conn.execute("SELECT sha256, status FROM files").fetchall())
        finally:
            conn.close()

    def record_count(self) -> int:
        conn = connect(self.db_path, "read_only")
        try:
            return conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
        finally:
            conn.close()

    def register(self, body: bytes, *, status: str = "new") -> str:
        """Save a raw file and its files row, as an earlier run would have left them."""
        conn = connect(self.db_path)
        try:
            migrate(conn)
            saved = save_raw(
                conn,
                self.data_dir,
                "health_auto_export",
                "rest_json",
                "json",
                io.BytesIO(body),
                now=NOW,
            )
            conn.execute("UPDATE files SET status = ? WHERE sha256 = ?", (status, saved.sha256))
        finally:
            conn.close()
        return saved.sha256

    def test_a_pushed_payload_is_read_and_its_records_are_stored(self):
        body = fixture("metrics.json")
        running = self.start_engine()

        self.push(body)
        running.drain()

        self.assertEqual(self.file_statuses(), {hashlib.sha256(body).hexdigest(): "ok"})
        self.assertEqual(self.record_count(), 8)

    def test_a_raw_file_with_no_row_is_registered_and_read_on_start(self):
        body = fixture("metrics.json")
        sha256 = hashlib.sha256(body).hexdigest()
        folder = self.data_dir / "raw" / "health_auto_export" / "rest_json"
        folder.mkdir(parents=True)
        (folder / f"{sha256}.json").write_bytes(body)

        self.start_engine().drain()

        self.assertEqual(self.file_statuses(), {sha256: "ok"})
        self.assertEqual(self.record_count(), 8)

    def test_a_failed_file_from_an_earlier_run_is_retried_once_on_start(self):
        sha256 = self.register(fixture("metrics.json"), status="failed")
        self.count_reads()

        self.start_engine().drain()

        self.assertEqual(self.file_statuses(), {sha256: "ok"})
        self.assertEqual(self.read_calls, [sha256])

    def test_a_failed_read_does_not_stop_the_next_push(self):
        broken, valid = fixture("workouts.json"), fixture("metrics.json")
        broken_sha256 = hashlib.sha256(broken).hexdigest()
        valid_sha256 = hashlib.sha256(valid).hexdigest()
        self.count_reads(fail_for=broken_sha256)
        running = self.start_engine()

        self.push(broken)
        running.drain()
        self.push(valid)
        running.drain()

        self.assertEqual(self.file_statuses(), {broken_sha256: "failed", valid_sha256: "ok"})
        self.assertEqual(self.read_calls, [broken_sha256, valid_sha256])

    def test_banner_without_home_wifi(self):
        self.assertEqual(
            render_banner("127.0.0.1", 47823, None, None, "tok", CONNECTORS),
            BANNER + TOKEN_LINE,
        )

    def test_banner_with_home_wifi(self):
        self.assertEqual(
            render_banner("127.0.0.1", 47823, "ana-pc.local", 47824, "tok", CONNECTORS),
            BANNER + HOME_WIFI_LINE + TOKEN_LINE,
        )

    def run_start(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                start,
                "Engine",
                lambda data_dir, **kwargs: Engine(data_dir, screen_port=free_port(), **kwargs),
            ),
            mock.patch.object(start, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(["start"])
        return code, out.getvalue(), err.getvalue()

    def test_start_prints_the_banner_and_exits_0_after_ctrl_c(self):
        code, out, _ = self.run_start()

        self.assertEqual(code, 0)
        token = password_store.get("push_token")
        self.assertEqual(
            out,
            render_banner(config.SCREEN_HOST, config.SCREEN_PORT, None, None, token, CONNECTORS)
            + "\n",
        )


if __name__ == "__main__":
    unittest.main()
