"""The one place that opens tfs.db."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROLES = ("default", "import", "save", "read_only")
WAL_ATTEMPTS = 10
WAL_WAIT_SECONDS = 0.05


def _switch_to_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Switch the file to WAL mode, retrying briefly while another process holds a lock."""
    # SQLite fails this switch at once instead of waiting for busy_timeout.
    for attempt in range(WAL_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error):
                raise
            if attempt == WAL_ATTEMPTS - 1:
                conn.close()
                raise sqlite3.OperationalError(
                    f"Could not switch {path} to WAL mode: the file is locked by another process."
                ) from error
            time.sleep(WAL_WAIT_SECONDS)


def connect(path: Path, role: str = "default") -> sqlite3.Connection:
    """Open the database for a role: default, import, save or read_only."""
    if role not in ROLES:
        raise ValueError(f"Unknown database role {role!r}. Use one of: {', '.join(ROLES)}.")
    if role == "read_only":
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, isolation_level=None)
        _switch_to_wal(conn, path)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if role == "save":
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
