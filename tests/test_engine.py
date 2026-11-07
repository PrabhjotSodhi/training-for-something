import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from tests.test_proposals import ADD, NOW, proposal
from tfs import config, engine, password_store
from tfs.connectors.core import send
from tfs.engine import Engine, EngineAlreadyRunningError, PortInUseError
from tfs.persistence.migrations import DatabaseTooNewError, migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import preview, save
from tfs.scheduling import jobs

IN_USE = "Port {port} is used by another app. Close it and try again."


def free_port() -> int:
    """Return a port that nothing holds right now."""
    with socket.socket() as sock:
        sock.bind((config.SCREEN_HOST, 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def holding(port: int, *, listen: bool) -> Iterator[None]:
    """Hold a port with a plain socket that never answers."""
    with socket.socket() as sock:
        sock.bind((config.SCREEN_HOST, port))
        if listen:
            sock.listen()
        yield


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = self.new_folder()
        for patch in (
            mock.patch.dict(
                os.environ, {"TFS_HOME": str(self.data_dir), "TFS_PASSWORD_STORE": "file"}
            ),
            mock.patch.object(engine, "HOME_WIFI_HOST", config.SCREEN_HOST),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def new_folder(self) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def new_engine(self, data_dir: Path | None = None, **options) -> Engine:
        options.setdefault("screen_port", free_port())
        running = Engine(data_dir or self.data_dir, **options)
        self.addCleanup(running.stop)
        return running

    def assert_port_free(self, port: int) -> None:
        with socket.socket() as sock:
            sock.bind((config.SCREEN_HOST, port))

    def test_a_second_engine_on_the_same_port_is_refused_and_leaves_its_folder_empty(self):
        port = free_port()
        self.new_engine(screen_port=port).start()
        other_folder = self.new_folder()
        second = self.new_engine(other_folder, screen_port=port)
        with self.assertRaises(EngineAlreadyRunningError) as caught:
            second.start()
        self.assertEqual(str(caught.exception), "TFS is already running.")
        self.assertFalse((other_folder / "tfs.db").exists())

    def test_a_port_held_by_another_app_is_refused(self):
        port = free_port()
        with (
            holding(port, listen=True),
            mock.patch.object(engine, "PING_TIMEOUT_S", 0.1),
            self.assertRaises(PortInUseError) as caught,
        ):
            self.new_engine(screen_port=port).start()
        self.assertEqual(str(caught.exception), IN_USE.format(port=port))
        self.assertFalse((self.data_dir / "tfs.db").exists())

    def test_a_taken_home_wifi_port_is_refused_and_the_screen_port_is_released(self):
        screen_port, home_wifi_port = free_port(), free_port()
        with holding(home_wifi_port, listen=False), self.assertRaises(PortInUseError) as caught:
            self.new_engine(
                home_wifi=True, screen_port=screen_port, home_wifi_port=home_wifi_port
            ).start()
        self.assertEqual(str(caught.exception), IN_USE.format(port=home_wifi_port))
        self.assert_port_free(screen_port)

    def test_start_creates_the_database_at_the_newest_schema_and_a_push_token(self):
        self.new_engine().start()
        conn = connect(self.data_dir / "tfs.db")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], migrate(conn))
        self.assertEqual(len(password_store.get("push_token") or ""), 43)

    def test_stop_ends_the_server_threads(self):
        before = set(threading.enumerate())
        running = self.new_engine(home_wifi=True, home_wifi_port=free_port())
        running.start()
        started = set(threading.enumerate()) - before
        self.assertEqual(len(started), 4)
        running.stop()
        self.assertFalse(any(thread.is_alive() for thread in started))

    def test_delivery_worker_picks_up_a_save_from_another_process(self) -> None:
        delivered = threading.Event()
        manifest = {"label": "Invented app", "sends": ["strength"], "asks": []}

        def helper(connector, op, payload, **options):
            self.assertEqual((connector, op), ("invented", "send"))
            self.assertEqual(payload["item"]["label"], "Push")
            delivered.set()
            return {"status": "sent", "remote_id": "routine-1", "remote_version": "version-1"}

        with (
            mock.patch.object(jobs, "load_manifests", return_value={"invented": manifest}),
            mock.patch.object(send.runner, "run", side_effect=helper),
        ):
            running = self.new_engine(now_fn=lambda: NOW)
            running.start()
            path = self.data_dir / "tfs.db"
            with contextlib.closing(connect(path)) as conn:
                conn.execute("INSERT INTO sources(connector) VALUES ('invented')")
            code = """
import json, sys
from datetime import datetime
from pathlib import Path
from tfs.proposals.apply import preview, save
from tfs.scheduling import jobs
jobs.load_manifests = lambda: json.loads(sys.argv[2])
path, value, now = Path(sys.argv[1]), json.loads(sys.argv[3]), datetime.fromisoformat(sys.argv[4])
value['delivery_targets'] = preview(path, value, now=now)['delivery_targets']
print(save(path, value, request_id='separate-save', approved_by='chat', now=now))
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(path),
                    json.dumps({"invented": manifest}),
                    json.dumps(proposal(ADD)),
                    NOW.isoformat(),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(delivered.wait(5), "The durable Save was not picked up.")
            running.stop()
            with contextlib.closing(connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "sent")
                self.assertEqual(
                    conn.execute("SELECT status FROM plan_items").fetchone()[0], "sent"
                )
                self.assertEqual(
                    conn.execute("SELECT remote_id FROM remote_links").fetchone()[0], "routine-1"
                )

    def test_stop_holds_exclusive_port_until_active_send_finishes(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        manifest = {"label": "Invented app", "sends": ["strength"], "asks": []}

        def helper(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Invented send was not released")
            return {"status": "sent", "remote_id": "routine-1", "remote_version": "version-1"}

        port = free_port()
        with (
            mock.patch.object(jobs, "load_manifests", return_value={"invented": manifest}),
            mock.patch.object(send.runner, "run", side_effect=helper) as call,
        ):
            running = self.new_engine(screen_port=port, now_fn=lambda: NOW)
            running.start()
            path = self.data_dir / "tfs.db"
            with contextlib.closing(connect(path)) as conn:
                conn.execute("INSERT INTO sources(connector) VALUES ('invented')")
            value = proposal(ADD)
            value["delivery_targets"] = preview(path, value, now=NOW)["delivery_targets"]
            save(path, value, request_id="save-1", approved_by="chat", now=NOW)
            self.assertTrue(entered.wait(5))
            stopping = threading.Thread(target=running.stop)
            stopping.start()
            try:
                self.assertTrue(running._stopping.wait(2))
                with self.assertRaises(EngineAlreadyRunningError):
                    self.new_engine(screen_port=port).start()
                self.assertEqual(call.call_count, 1)
            finally:
                release.set()
                stopping.join(5)
            self.assertFalse(stopping.is_alive())
            restarted = self.new_engine(screen_port=port, now_fn=lambda: NOW)
            restarted.start()
            restarted.stop()
            self.assertEqual(call.call_count, 1)
            with contextlib.closing(connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "sent")

    def test_an_existing_push_token_is_kept(self):
        password_store.set("push_token", "token-from-an-earlier-run")
        self.new_engine().start()
        self.assertEqual(password_store.get("push_token"), "token-from-an-earlier-run")

    def test_a_failure_after_the_ports_are_bound_releases_them(self):
        conn = connect(self.data_dir / "tfs.db")
        conn.execute("PRAGMA user_version = 999")
        conn.close()
        screen_port, home_wifi_port = free_port(), free_port()
        with self.assertRaises(DatabaseTooNewError):
            self.new_engine(
                home_wifi=True, screen_port=screen_port, home_wifi_port=home_wifi_port
            ).start()
        self.assert_port_free(screen_port)
        self.assert_port_free(home_wifi_port)
