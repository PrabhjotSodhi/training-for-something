"""Validate the supported session actions and calculate their resulting plan."""

import copy
import json
import math
import sqlite3
from datetime import date
from urllib.parse import urlsplit

from tfs.connectors.core.contract import FAMILIES
from tfs.proposals import journal

FIELDS = {
    "add_session": ({"action", "item", "date", "family", "label", "parts"}, {"goal_ids"}),
    "move_session": ({"action", "item", "to"}, set()),
    "change_session": ({"action", "item"}, {"label", "parts"}),
    "remove_session": ({"action", "item"}, set()),
    "mark_done": ({"action", "item"}, set()),
}
PART_FIELDS = {"part_kind", "label", "duration_s", "distance_m", "reps", "weight_kg", "avg_hr"}


def _fields(value: object, required: set, optional: set = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise ValueError("Missing or unknown fields.")


def _text(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text.")


def _day(value: object, today: date) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected an ISO local date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Expected an ISO local date.") from error
    if parsed.isoformat() != value or parsed < today:
        raise ValueError("A planned date must be today or later, in YYYY-MM-DD form.")
    return value


def _parts(value: object) -> None:
    if not isinstance(value, list):
        raise ValueError("Parts must be a list.")
    for part in value:
        _fields(part, {"part_kind"}, PART_FIELDS - {"part_kind"})
        _text(part["part_kind"])
        if "label" in part:
            _text(part["label"])
        for field in PART_FIELDS - {"part_kind", "label"}:
            if field not in part:
                continue
            number = part[field]
            if (
                type(number) not in (int, float)
                or not 0 <= number <= 1e308
                or not math.isfinite(number)
            ):
                raise ValueError(f"{field} must be a finite non-negative number.")
            if field == "reps" and (type(number) is not int or number > 2**63 - 1):
                raise ValueError("reps must be a whole number.")


def _load(conn: sqlite3.Connection, item: str) -> dict | None:
    row = conn.execute("SELECT * FROM plan_items WHERE id = ?", (item,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["goal_ids"] = json.loads(result.pop("goal_ids_json"))
    result["sources"] = json.loads(result.pop("sources_json"))
    result["parts"] = [
        {key: value for key, value in dict(part).items() if value is not None}
        for part in conn.execute(
            "SELECT part_kind, label, duration_s, distance_m, reps, weight_kg, avg_hr, extras_json"
            " FROM session_parts WHERE plan_item_id = ? ORDER BY seq",
            (item,),
        )
    ]
    return result


def _proposal(conn: sqlite3.Connection, proposal: dict) -> None:
    _fields(proposal, {"plan_version", "why", "sources", "actions"})
    expected = proposal["plan_version"]
    if type(expected) is not int or expected < 0 or expected != journal.version(conn):
        raise ValueError("The proposal is stale. Read the current plan and propose again.")
    _text(proposal["why"])
    if not isinstance(proposal["sources"], list):
        raise ValueError("Sources must be a list.")
    for source in proposal["sources"]:
        _fields(source, {"url", "type"})
        _text(source["url"])
        _text(source["type"])
        link = urlsplit(source["url"])
        if link.scheme not in ("https", "http") or not link.netloc:
            raise ValueError("Each source needs an HTTP or HTTPS URL.")
    if not isinstance(proposal["actions"], list) or not proposal["actions"]:
        raise ValueError("A proposal needs at least one action.")


def _add(conn: sqlite3.Connection, action: dict, today: date) -> dict:
    if action["family"] not in FAMILIES:
        raise ValueError("Unknown sport family.")
    _text(action["label"])
    _parts(action["parts"])
    goals = action.get("goal_ids", [])
    if not isinstance(goals, list):
        raise ValueError("goal_ids must be a list.")
    for goal in goals:
        _text(goal)
        if conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal,)).fetchone() is None:
            raise ValueError("Unknown goal.")
    return {
        "id": action["item"],
        "version": 0,
        "local_date": _day(action["date"], today),
        "start_hint": None,
        "family": action["family"],
        "label": action["label"],
        "goal_ids": list(dict.fromkeys(goals)),
        "status": "saved",
        "parts": copy.deepcopy(action["parts"]),
    }


def _edit(state: dict | None, action: dict, today: date) -> None:
    if state is None or state["status"] in ("done", "removed", "replaced"):
        raise ValueError("The plan item is missing or can no longer be changed.")
    name = action["action"]
    if name == "move_session":
        state["local_date"] = _day(action["to"], today)
    elif name == "change_session":
        if not ({"label", "parts"} & action.keys()):
            raise ValueError("A change needs a label or parts.")
        if "label" in action:
            _text(action["label"])
            state["label"] = action["label"]
        if "parts" in action:
            _parts(action["parts"])
            state["parts"] = copy.deepcopy(action["parts"])
    else:
        state["status"] = "removed" if name == "remove_session" else "done"


def prepare(conn: sqlite3.Connection, proposal: dict, today: date) -> tuple[dict, dict]:
    """Validate a batch in order, returning original and resulting item states."""
    _proposal(conn, proposal)
    before, after = {}, {}
    for action in proposal["actions"]:
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise ValueError("Each action needs a known action name.")
        name = action["action"]
        if name not in FIELDS:
            raise ValueError(f"Unsupported action: {name}.")
        _fields(action, *FIELDS[name])
        item = action["item"]
        _text(item)
        if item not in before:
            before[item] = _load(conn, item)
            after[item] = copy.deepcopy(before[item])
        if name == "add_session":
            if after[item] is not None:
                raise ValueError("This plan item already exists.")
            after[item] = _add(conn, action, today)
        else:
            _edit(after[item], action, today)
        after[item]["why"] = proposal["why"]
        after[item]["sources"] = copy.deepcopy(proposal["sources"])
    for state in after.values():
        state["version"] += 1
        state["plan_version"] = proposal["plan_version"] + 1
    return before, after
