"""The one place that opens tfs.db."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

ROLES = ("default", "import", "save", "read_only")
WAL_WAIT_SECONDS = 5.0
WAL_RETRY_SECONDS = 0.05


def _switch_to_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Switch the file to WAL mode, retrying briefly while another process holds a lock."""
    # The connection has no busy timeout yet, so this loop is the whole wait: at most
    # about 5 seconds, like the busy timeout set later. A timeout above 0 would let
    # each attempt wait that long on its own.
    deadline = time.monotonic() + WAL_WAIT_SECONDS
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error):
                raise
            if time.monotonic() >= deadline:
                conn.close()
                raise sqlite3.OperationalError(
                    f"Could not switch {path} to WAL mode: the file is locked by another process."
                ) from error
            time.sleep(WAL_RETRY_SECONDS)


def connect(path: Path, role: str = "default") -> sqlite3.Connection:
    """Open the database for a role: default, import, save or read_only."""
    if role not in ROLES:
        raise ValueError(f"Unknown database role {role!r}. Use one of: {', '.join(ROLES)}.")
    if role == "read_only":
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, isolation_level=None, timeout=0)
        _switch_to_wal(conn, path)
        conn.execute("PRAGMA foreign_keys=ON")
        # The next writer trims a checkpointed log to 64 MiB when it reuses it.
        conn.execute("PRAGMA journal_size_limit=67108864")
    # Each writer flushes its own work so a Save never has to flush earlier import batches.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if role == "import":
        # Leave checkpoints to the end of the import unless the log reaches a million pages.
        conn.execute("PRAGMA wal_autocheckpoint=1000000")
    elif role == "save":
        # A Save never runs a checkpoint, so its speed does not depend on the disk.
        # SQLite also checkpoints when the last connection closes, so turn that off too.
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one write transaction: commit on success, roll back and re-raise on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # SQLite may already have ended the transaction, for example on a full disk.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def save_intent(path: Path) -> Iterator[None]:
    """Keep imports between batches until this Save finishes or its process exits."""
    control = Path(str(path.resolve()) + ".save-priority")
    # An empty rollback-mode file holds locks only, never user data. Do not unlink it:
    # other processes may still hold its inode open.
    with closing(sqlite3.connect(control, isolation_level=None, timeout=5)) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT name FROM sqlite_schema").fetchall()
        yield


def wait_for_saves(conn: sqlite3.Connection) -> None:
    """Yield before an import transaction while another process announces a Save."""
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not path:
        return
    control = Path(str(Path(path).resolve()) + ".save-priority")
    with closing(sqlite3.connect(control, isolation_level=None, timeout=0)) as probe:
        while True:
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.rollback()
                return
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                time.sleep(0.005)
