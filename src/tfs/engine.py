"""The background process: it claims its port first, then prepares the data, then serves."""

import contextlib
import http.client
import logging
import queue
import secrets
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from tfs import password_store, scores
from tfs.config import HOME_WIFI_PORT, SCREEN_HOST, SCREEN_PORT
from tfs.connectors.core.contract import to_utc_text
from tfs.importer.raw_store import scan_raw
from tfs.importer.session import process_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect, transaction
from tfs.scheduling import jobs, scheduler
from tfs.web.overview import Overview, read_overview
from tfs.web.push import make_push_handler
from tfs.web.server import PushHandler, WebServer, make_server

HOME_WIFI_HOST = "0.0.0.0"
PING_TIMEOUT_S = 1
SCHEDULER_INTERVAL_S = 60

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
        self._running: list[WebServer] = []
        # Each job is the retry_failed flag for one process_pending run. None ends the worker.
        self._jobs: queue.Queue[bool | None] = queue.Queue()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        self._delivery_worker: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None

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

        def overview() -> Overview:
            return read_overview(db_path, self._now_fn().astimezone(self._tz).date())

        servers: list[WebServer] = []
        try:
            servers.append(
                _bind(SCREEN_HOST, self._screen_port, handler, home_wifi=False, overview=overview)
            )
            if self._home_wifi:
                servers.append(_bind(HOME_WIFI_HOST, self._home_wifi_port, handler, home_wifi=True))
            self._prepare_data(db_path)
            for server in servers:
                server.start()
            worker = threading.Thread(target=self._work)
            worker.start()
        except BaseException:
            for server in servers:
                server.stop()
            raise
        self._running = servers
        self._worker = worker
        self._jobs.put(True)
        self._delivery_worker = threading.Thread(target=self._deliver)
        self._delivery_worker.start()
        self._scheduler = threading.Thread(target=self._schedule)
        self._scheduler.start()

    def stop(self) -> None:
        """Finish active workers before releasing the exclusive engine port."""
        self._stopping.set()
        running, self._running = self._running, []
        worker, self._worker = self._worker, None
        if worker is not None:
            self._jobs.put(None)
            worker.join()
        delivery, self._delivery_worker = self._delivery_worker, None
        if delivery is not None:
            delivery.join()
        scheduler_thread, self._scheduler = self._scheduler, None
        if scheduler_thread is not None:
            scheduler_thread.join()
        for server in running:
            server.stop()

    def drain(self) -> None:
        """Block until the startup import and queued file reads have finished."""
        self._jobs.join()

    def _work(self) -> None:
        while (retry_failed := self._jobs.get()) is not None:
            if not self._stopping.is_set():
                self._process(retry_failed)
            self._jobs.task_done()
        self._jobs.task_done()

    def _process(self, retry_failed: bool) -> None:
        with contextlib.suppress(Exception):
            with contextlib.closing(connect(self._data_dir / "tfs.db", "import")) as conn:
                process_pending(conn, self._data_dir, tz=self._tz, retry_failed=retry_failed)
            return
        logger.error("Reading new files failed. TFS tries again after the next push.")

    def _deliver(self) -> None:
        while not self._stopping.is_set():
            try:
                worked = jobs.run_one(self._data_dir / "tfs.db", to_utc_text(self._now_fn()))
            except (OSError, ValueError, sqlite3.Error):
                logger.error("Delivery processing failed. Interrupted sends need reconciliation.")
                worked = False
            if not worked:
                self._stopping.wait(1)

    def _schedule(self) -> None:
        while not self._stopping.is_set():
            try:
                with contextlib.closing(connect(self._data_dir / "tfs.db", "save")) as conn:
                    scheduler.run_due(conn, to_utc_text(self._now_fn()))
            except (OSError, ValueError, sqlite3.Error):
                logger.error("Scheduler run failed. TFS tries again in a minute.")
            self._stopping.wait(SCHEDULER_INTERVAL_S)

    def _prepare_data(self, db_path: Path) -> None:
        (self._data_dir / "raw").mkdir(parents=True, exist_ok=True)
        with contextlib.closing(connect(db_path)) as conn:
            migrate(conn)
            with transaction(conn):
                scores.rebuild_if_outdated(conn)
            scan_raw(conn, self._data_dir)
            jobs.recover(conn)
        if not password_store.get("push_token"):
            password_store.set("push_token", secrets.token_urlsafe(32))


def _bind(
    host: str,
    port: int,
    handler: PushHandler,
    *,
    home_wifi: bool,
    overview: Callable[[], Overview] | None = None,
) -> WebServer:
    """Bind a server. A taken port raises PortInUseError, or EngineAlreadyRunningError if TFS holds it."""
    try:
        return make_server(host, port, push_handler=handler, push_only=home_wifi, overview=overview)
    except OSError as error:
        if not home_wifi and _tfs_answers_ping(port):
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
