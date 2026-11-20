"""Validate and store user-recorded goals and training constraints."""

import copy
import json
import sqlite3
from datetime import date

from tfs.proposals import journal


def _day(value: str, today: date, future: bool = False) -> str:
    parsed = date.fromisoformat(value)
    if parsed < today or (future and parsed == today):
        raise ValueError("Use a future YYYY-MM-DD date for a goal, or today or later for a limit.")
    return value


def validate(action: dict, today: date) -> dict:
    """Return a goal or limit state. The proposal model has checked every field but the dates."""
    if action["action"] == "set_goal":
        return {
            "id": action["item"],
            "type": action["type"],
            "local_date": _day(action["date"], today, True),
            "target": copy.deepcopy(action["target"]),
        }
    kind, values = action["kind"], action["values"]
    if kind == "unavailable_day":
        _day(values["date"], today)
    return {"id": action["item"], "kind": kind, "values": copy.deepcopy(values)}


def load(conn: sqlite3.Connection, table: str, item: str) -> dict | None:
    """Read one goal or limit, decoding its stored JSON."""
    sql = {
        "goals": "SELECT * FROM goals WHERE id = ?",
        "limits": "SELECT * FROM limits WHERE id = ?",
    }
    row = conn.execute(sql[table], (item,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    field = "target" if table == "goals" else "values"
    result[field] = json.loads(result.pop(field + "_json"))
    return result


def write(conn: sqlite3.Connection, changes: dict) -> None:
    """Apply validated goal and limit states in the caller's transaction."""
    for item, state in changes["goals"].items():
        if state is None:
            conn.execute("DELETE FROM goals WHERE id = ?", (item,))
        else:
            conn.execute(
                "INSERT INTO goals (id, type, local_date, target_json) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET type=excluded.type, local_date=excluded.local_date, target_json=excluded.target_json",
                (item, state["type"], state["local_date"], journal.encode(state["target"])),
            )
    for item, state in changes["limits"].items():
        if state is None:
            conn.execute("DELETE FROM limits WHERE id = ?", (item,))
        else:
            conn.execute(
                "INSERT INTO limits (id, kind, values_json) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, values_json=excluded.values_json",
                (item, state["kind"], journal.encode(state["values"])),
            )
