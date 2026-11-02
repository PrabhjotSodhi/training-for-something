"""The background process: it claims its port first, then prepares the data, then serves."""

import contextlib
import http.client
import logging
import queue
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from http.server import ThreadingHTTPServer
from pathlib import Path

from tfs import password_store
from tfs.config import HOME_WIFI_PORT, SCREEN_HOST, SCREEN_PORT
from tfs.connectors.core.contract import to_utc_text
from tfs.importer.raw_store import scan_raw
from tfs.importer.session import process_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.web.push import make_push_handler
from tfs.web.server import PushHandler, make_server

HOME_WIFI_HOST = "0.0.0.0"
PING_TIMEOUT_S = 1

logger = logging.getLogger(__name__)


class EngineAlreadyRunningError(Exception):
    """Another TFS engine already holds the screen port."""


class PortInUseError(Exception):
    """Another app holds a port TFS needs."""


class Engine:
    """One TFS engine: a screen server, with home_wifi a second server that takes pushes, and a worker that reads what they save."""

    def __init__(
        self,
        data_dir: Path,
        *,
        home_wifi: bool = False,
        screen_port: int = SCREEN_PORT,
        home_wifi_port: int = HOME_WIFI_PORT,
        now_fn: Callable[[], datetime] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._home_wifi = home_wifi
        self._screen_port = screen_port
        self._home_wifi_port = home_wifi_port
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._tz = tz
        self._running: list[tuple[ThreadingHTTPServer, threading.Thread]] = []
        # Each job is the retry_failed flag for one process_pending run. None ends the worker.
        self._jobs: queue.Queue[bool | None] = queue.Queue()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        """Claim the ports, prepare the data and start serving. On any failure, release the ports."""
        self._stopping.clear()
        db_path = self._data_dir / "tfs.db"
        handler = make_push_handler(
            data_dir=self._data_dir,
            conn_factory=lambda: connect(db_path, "save"),
            now_fn=self._now_fn,
            on_saved=lambda sha256: self._jobs.put(False),
        )
        servers: list[ThreadingHTTPServer] = []
        running: list[tuple[ThreadingHTTPServer, threading.Thread]] = []
        try:
            servers.append(_bind(SCREEN_HOST, self._screen_port, handler, ask_ping=True))
            if self._home_wifi:
                servers.append(_bind(HOME_WIFI_HOST, self._home_wifi_port, handler, ask_ping=False))
            self._prepare_data(db_path)
            for server in servers:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                running.append((server, thread))
            worker = threading.Thread(target=self._work)
            worker.start()
        except BaseException:
            _stop(running)
            # shutdown() waits forever on a server whose thread never started.
            for server in servers[len(running) :]:
                server.server_close()
            raise
        self._running = running
        self._worker = worker
        self._jobs.put(True)

    def stop(self) -> None:
        """Stop serving, release the ports, let the worker finish the file it is reading and join it."""
        running, self._running = self._running, []
        _stop(running)
        worker, self._worker = self._worker, None
        if worker is not None:
            self._stopping.set()
            self._jobs.put(None)
            worker.join()

    def drain(self) -> None:
        """Block until the startup run, the queue and the job in progress have finished."""
        self._jobs.join()

    def _work(self) -> None:
        while (retry_failed := self._jobs.get()) is not None:
            if not self._stopping.is_set():
                self._process(retry_failed)
            self._jobs.task_done()
        self._jobs.task_done()

    def _process(self, retry_failed: bool) -> None:
        with contextlib.suppress(Exception):
            conn = connect(self._data_dir / "tfs.db", "import")
            try:
                process_pending(
                    conn,
                    self._data_dir,
                    now=to_utc_text(self._now_fn()),
                    tz=self._tz,
                    retry_failed=retry_failed,
                )
            finally:
                conn.close()
            return
        logger.error("Reading new files failed. TFS tries again after the next push.")

    def _prepare_data(self, db_path: Path) -> None:
        (self._data_dir / "raw").mkdir(parents=True, exist_ok=True)
        conn = connect(db_path)
        try:
            migrate(conn)
            scan_raw(conn, self._data_dir, now=to_utc_text(self._now_fn()))
        finally:
            conn.close()
        if not password_store.get("push_token"):
            password_store.set("push_token", secrets.token_urlsafe(32))


def _stop(running: list[tuple[ThreadingHTTPServer, threading.Thread]]) -> None:
    for server, _ in running:
        server.shutdown()
    for server, thread in running:
        server.server_close()
        thread.join()


def _bind(host: str, port: int, handler: PushHandler, *, ask_ping: bool) -> ThreadingHTTPServer:
    """Bind a server. A taken port raises PortInUseError, or EngineAlreadyRunningError if TFS holds it."""
    try:
        return make_server(host, port, push_handler=handler)
    except OSError as error:
        if ask_ping and _tfs_answers_ping(port):
            raise EngineAlreadyRunningError("TFS is already running.") from error
        raise PortInUseError(
            f"Port {port} is used by another app. Close it and try again."
        ) from error


def _tfs_answers_ping(port: int) -> bool:
    connection = http.client.HTTPConnection(SCREEN_HOST, port, timeout=PING_TIMEOUT_S)
    try:
        connection.request("GET", "/ping")
        response = connection.getresponse()
        return response.status == 200 and response.read(4) == b"tfs"
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
