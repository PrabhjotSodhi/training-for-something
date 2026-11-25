"""The one guarded, read-only way an AI tool reads data (HLD 9.2)."""

import json
import sqlite3
import time
from contextlib import closing
from importlib import resources
from pathlib import Path
from typing import Any

from tfs.persistence import sqlite

VIEWS = {
    "coach_v1_deliveries": (
        "One row per approved delivery job, including cancelled history. op_id is stable."
        " Times are UTC; unresolved unknown/conflict/needs_you require user action. Remote fields"
        " identify that job's recorded result, falling back to the current link while pending."
        " manual_action=delete is a manual task: the person deletes the copy in the app, and TFS"
        " no longer tracks it. safe_to_replace=1 means this"
        " needs_you operation failed before writing and can be superseded by an approved edit."
        " Sending never means training done."
    ),
    "coach_v1_day_facts": (
        "One row per day and metric: the day's value, unit and source."
        " Coverage is full, partial or importing."
    ),
    "coach_v1_scores": (
        "One row per day and score. value is 1 to 99 with 50 as the person's normal, and null"
        " when data is missing; low and high are its likely range. inputs_json shows each"
        " input's working or the reason it was left out."
    ),
    "coach_v1_workouts": (
        "One row per workout, shown once even when several apps recorded it."
        " start_utc is UTC and utc_offset_min gives local time. source names the app whose"
        " duration and distance were chosen, and each *_source column names the app of that"
        " value. sources lists every app that recorded it. conflict is 1 when the apps"
        " disagree on duration or distance by more than 10%."
    ),
    "coach_v1_workout_parts": (
        "The sets, laps or intervals of a workout, in order by seq, from one app."
    ),
    "coach_v1_sets_by_muscle": (
        "Working sets per primary muscle for each week, from apps that tag exercises, such as"
        " Hevy. week_start is the Monday. Warmup sets are left out. An exercise with no muscle"
        " tag counts under other. A week with no sets has no row."
    ),
    "coach_v1_sources": (
        "One row per connected app: whether it is enabled, when its last file arrived, its"
        " last error, and the latest time its data covers."
    ),
    "coach_v1_plan_items": (
        "One row per saved plan item, including removed items; filter status as needed."
        " version is the item's version; plan_version is the global version at its last"
        " change. goal_ids_json and sources_json hold JSON arrays; why explains the plan."
        " local_date is YYYY-MM-DD and start_hint is an optional local time. send_into_json"
        " maps a connector to the coach_v1_app_targets remote_id the session goes into."
        " status includes automatic workout completion; matched_workout_id and match_reason"
        " identify its evidence. matched_record_ids_json lists its active source IDs, sorted,"
        " for unlink_plan_match. Pending rebuilds hide automatic matches; manual done remains."
    ),
    "coach_v1_plan_parts": (
        "Planned targets in seq order, never imported workout parts. Duration is in minutes,"
        " distance in km, weight in kg and avg_hr in beats/minute; missing values stay null."
        " extras_json holds any additional stored part details."
    ),
    "coach_v1_plan_state": (
        "Exactly one row: the current global plan_version to use for a proposal,"
        " or zero before the first Save. This can exceed an unchanged item's plan_version."
    ),
    "coach_v1_goals": (
        "One row per user-approved goal. target_json holds its description and optional"
        " value/unit pair in the saved units. local_date is its target date, not a forecast."
    ),
    "coach_v1_forecasts": (
        "One row per goal with a coach estimate, the latest Save only. low and high bound the"
        " range in unit. Show it as the label, with its method, sources_json and local_date."
    ),
    "coach_v1_overview": (
        "At most one row: the saved Overview layout as JSON, a plate and a list of cards."
        " No row means the default layout."
    ),
    "coach_v1_limits": (
        "One row per user-approved training constraint. values_json holds the kind's"
        " fields, such as minutes, a date or user-recorded injury text; not a diagnosis."
    ),
    "coach_v1_app_targets": (
        "One row per object in a connected app that a session can be sent into, such as a"
        " Hevy routine, as of the last sync. folder is the app's folder ID. Name one in"
        " send_into on add_session or change_session."
    ),
}
MAX_ROWS = 500
MAX_BYTES = 200_000
MAX_SECONDS = 2
PROGRESS_STEPS = 1000


# The source authorizer permits view dependencies. query() first prepares the
# exact SQL against a coach-only schema, so a same-name CTE cannot name raw tables.
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


def _preflight(conn: sqlite3.Connection, sql: str, deadline: float) -> None:
    """Let SQLite resolve user SQL without any raw tables in its namespace."""
    columns = {name: info["columns"] for name, info in describe(conn).items()}
    with closing(sqlite.connect(Path(":memory:"))) as check:
        for name, fields in columns.items():
            names = ", ".join('"' + field.replace('"', '""') + '"' for field in fields)
            check.execute(f'CREATE TEMP TABLE "{name}" ({names})')

        def authorize(action, table, column, db, source):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (column or "").lower() != "load_extension":
                return sqlite3.SQLITE_OK
            # An empty-schema CTE count has no database name or column. Raw
            # tables cannot resolve here; SQLite's own schema remains forbidden.
            if (
                action == sqlite3.SQLITE_READ
                and db is None
                and not column
                and not table.lower().startswith("sqlite_")
            ):
                return sqlite3.SQLITE_OK
            if (
                action == sqlite3.SQLITE_READ
                and table in columns
                and db in ("temp", None)
                and (not column or column in columns[table])
            ):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        check.set_authorizer(authorize)
        check.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
        check.execute("EXPLAIN " + sql)


def query(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    """Run one SELECT within the row, byte and time limits, or return an error."""
    deadline = time.monotonic() + MAX_SECONDS
    conn.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_STEPS)
    try:
        _preflight(conn, sql, deadline)
        if time.monotonic() > deadline:
            return {"error": f"The query ran longer than {MAX_SECONDS} seconds."}
        cursor = conn.execute(sql)
        columns = [column[0] for column in cursor.description or []]
        rows: list[list[Any]] = []
        clipped = None
        size = len(
            json.dumps(
                {"columns": columns, "rows": [], "clipped": f"Stopped at {MAX_BYTES:,} bytes."}
            )
        )
        for values in cursor:
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
    except TypeError:
        return {"error": "A result held binary data, which cannot be shown."}
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code == sqlite3.SQLITE_AUTH or str(error).startswith(
            ("no such table:", "not authorized")
        ):
            return {"error": "Only coach_v1 views can be read."}
        if code == sqlite3.SQLITE_INTERRUPT:
            return {"error": f"The query ran longer than {MAX_SECONDS} seconds."}
        return {"error": str(error)}
    finally:
        conn.set_progress_handler(None, 0)
