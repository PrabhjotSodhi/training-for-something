import contextlib
import hashlib
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from tfs import config, engine, password_store
from tfs.engine import Engine, EngineAlreadyRunningError, PortInUseError
from tfs.persistence.migrations import DatabaseTooNewError, migrate
from tfs.persistence.sqlite import connect

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
        self.assertGreater(len(password_store.get("push_token") or ""), 30)

    def test_stop_ends_the_server_threads(self):
        before = set(threading.enumerate())
        running = self.new_engine(home_wifi=True, home_wifi_port=free_port())
        running.start()
        started = set(threading.enumerate()) - before
        self.assertEqual(len(started), 2)
        running.stop()
        self.assertFalse(any(thread.is_alive() for thread in started))

    def test_an_existing_push_token_is_kept(self):
        password_store.set("push_token", "token-from-an-earlier-run")
        self.new_engine().start()
        self.assertEqual(password_store.get("push_token"), "token-from-an-earlier-run")

    def test_start_registers_raw_files_that_have_no_row(self):
        data = b'{"workouts": []}'
        sha256 = hashlib.sha256(data).hexdigest()
        folder = self.data_dir / "raw" / "hevy" / "workouts_page"
        folder.mkdir(parents=True)
        (folder / f"{sha256}.json").write_bytes(data)
        self.new_engine().start()
        conn = connect(self.data_dir / "tfs.db")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT sha256 FROM files").fetchall()[0]["sha256"], sha256)

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

    def test_a_server_thread_that_cannot_start_leaves_nothing_running(self):
        screen_port, home_wifi_port = free_port(), free_port()
        started: list[threading.Thread] = []
        real_start = threading.Thread.start

        def start_one_only(thread: threading.Thread) -> None:
            if started:
                raise RuntimeError("can't start new thread")
            started.append(thread)
            real_start(thread)

        with (
            mock.patch.object(threading.Thread, "start", start_one_only),
            self.assertRaises(RuntimeError),
        ):
            self.new_engine(
                home_wifi=True, screen_port=screen_port, home_wifi_port=home_wifi_port
            ).start()
        self.assertFalse(started[0].is_alive())
        self.assert_port_free(screen_port)
        self.assert_port_free(home_wifi_port)
