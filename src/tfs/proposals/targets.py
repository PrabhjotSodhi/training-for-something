"""Validate and store user-recorded goals and training constraints."""

import copy
import json
import math
import sqlite3
from datetime import date

from tfs.proposals import journal

GOAL_TYPES = (
    "marathon",
    "ironman",
    "hyrox",
    "powerlifting",
    "lean_physique",
    "longevity",
    "custom",
)


def _text(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text.")


def _fields(value: object, required: set, optional: set = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise ValueError("Missing or unknown goal/limit fields.")


def _number(value: object) -> None:
    if type(value) not in (int, float) or not 0 <= value <= 1e308 or not math.isfinite(value):
        raise ValueError("A target must be a finite non-negative number.")


def _day(value: object, today: date, future: bool = False) -> str:
    _text(value)
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed < today or (future and parsed == today):
        raise ValueError("Use a future YYYY-MM-DD date for a goal, or today or later for a limit.")
    return value


def validate(action: dict, today: date) -> dict:
    """Return a goal or limit state after validating its documented fields."""
    if action["action"] == "set_goal":
        if action["type"] not in GOAL_TYPES:
            raise ValueError("Unknown goal type; use custom for another goal.")
        target = action["target"]
        _fields(target, {"description"}, {"value", "unit"})
        _text(target["description"])
        if ("value" in target) != ("unit" in target):
            raise ValueError("A numeric goal target needs both value and unit.")
        if "value" in target:
            _number(target["value"])
            _text(target["unit"])
        return {
            "id": action["item"],
            "type": action["type"],
            "local_date": _day(action["date"], today, True),
            "target": copy.deepcopy(target),
        }
    kind, values = action["kind"], action["values"]
    if kind == "injury":
        _fields(values, {"description"})
        _text(values["description"])
    elif kind == "unavailable_day":
        _fields(values, {"date"})
        _day(values["date"], today)
    elif kind == "max_session_length":
        _fields(values, {"minutes"})
        _number(values["minutes"])
        if values["minutes"] == 0:
            raise ValueError("Maximum session length must be positive.")
    else:
        raise ValueError("Unknown training limit kind.")
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
