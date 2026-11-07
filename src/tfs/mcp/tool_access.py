"""The one guarded, read-only way an AI tool reads data (HLD 9.2)."""

import json
import sqlite3
import time
from importlib import resources
from pathlib import Path
from typing import Any

from tfs.persistence import sqlite

VIEWS = {
    "coach_v1_day_facts": (
        "One row per day and metric: the day's value, unit and source."
        " Coverage is full, partial or importing."
    ),
}
MAX_ROWS = 500
MAX_BYTES = 200_000
MAX_SECONDS = 2
PROGRESS_STEPS = 1000


def _authorize(
    action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None
) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and (
        not arg2 or (db == "temp" and arg1 in VIEWS) or source in VIEWS
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_reader(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection that can read only the coach_v1 views."""
    conn = sqlite.connect(db_path, "read_only")
    conn.executescript(resources.files("tfs.mcp").joinpath("views.sql").read_text("utf-8"))
    # SQLite reports a view's own columns with no fifth argument, and a CTE's name also
    # arrives as the fifth argument. So a CTE named exactly like a view can read a base
    # table, and count(*) works on any table. Both only read, and the AI tool already
    # runs on the person's own computer (HLD section 12).
    conn.set_authorizer(_authorize)
    return conn


def describe(conn: sqlite3.Connection) -> dict[str, Any]:
    """List each coach_v1 view with its meaning and column names."""
    conn.set_authorizer(None)
    try:
        return {
            name: {
                "meaning": meaning,
                "columns": [
                    row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (name,))
                ],
            }
            for name, meaning in VIEWS.items()
        }
    finally:
        conn.set_authorizer(_authorize)


def query(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    """Run one SELECT within the row, byte and time limits, or return an error."""
    deadline = time.monotonic() + MAX_SECONDS
    conn.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
    try:
        cursor = conn.execute(sql)
        columns = [column[0] for column in cursor.description or []]
        rows: list[list[Any]] = []
        clipped = None
        size = len(json.dumps({"columns": columns, "rows": [], "clipped": None}))
        for values in cursor.fetchmany(MAX_ROWS + 1):
            row = list(values)
            if len(rows) == MAX_ROWS:
                clipped = f"Stopped at {MAX_ROWS} rows."
                break
            size += len(json.dumps(row)) + 2
            if size > MAX_BYTES:
                clipped = f"Stopped at {MAX_BYTES:,} bytes."
                break
            rows.append(row)
        return {"columns": columns, "rows": rows, "clipped": clipped}
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code == sqlite3.SQLITE_AUTH:
            return {"error": "Only coach_v1 views can be read."}
        if code == sqlite3.SQLITE_INTERRUPT:
            return {"error": f"The query ran longer than {MAX_SECONDS} seconds."}
        return {"error": str(error)}
    finally:
        conn.set_progress_handler(None, 0)
