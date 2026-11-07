"""Validate supported proposal actions and calculate their resulting user state."""

import copy
import json
import math
import sqlite3
from datetime import date
from urllib.parse import urlsplit

from tfs.connectors.core.contract import FAMILIES
from tfs.proposals import journal, targets

FIELDS = {
    "add_session": ({"action", "item", "date", "family", "label", "parts"}, {"goal_ids"}),
    "move_session": ({"action", "item", "to"}, set()),
    "change_session": ({"action", "item"}, {"label", "parts"}),
    "remove_session": ({"action", "item"}, set()),
    "mark_done": ({"action", "item"}, set()),
    "set_goal": ({"action", "item", "type", "date", "target"}, set()),
    "set_limit": ({"action", "item", "kind", "values"}, set()),
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


def _add(conn: sqlite3.Connection, action: dict, today: date, pending_goals: dict) -> dict:
    if action["family"] not in FAMILIES:
        raise ValueError("Unknown sport family.")
    _text(action["label"])
    _parts(action["parts"])
    goals = action.get("goal_ids", [])
    if not isinstance(goals, list):
        raise ValueError("goal_ids must be a list.")
    for goal in goals:
        _text(goal)
        if (
            goal not in pending_goals
            and conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal,)).fetchone() is None
        ):
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


def _session(
    conn: sqlite3.Connection, action: dict, today: date, before: dict, after: dict
) -> None:
    item = action["item"]
    if item not in before["items"]:
        before["items"][item] = _load(conn, item)
        after["items"][item] = copy.deepcopy(before["items"][item])
    if action["action"] == "add_session":
        if after["items"][item] is not None:
            raise ValueError("This plan item already exists.")
        after["items"][item] = _add(conn, action, today, after["goals"])
    else:
        _edit(after["items"][item], action, today)


def prepare(conn: sqlite3.Connection, proposal: dict, today: date) -> tuple[dict, dict]:
    """Validate a batch in order, returning original and resulting user states."""
    _proposal(conn, proposal)
    before = {"items": {}, "goals": {}, "limits": {}}
    after = copy.deepcopy(before)
    for action in proposal["actions"]:
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise ValueError("Each action needs a known action name.")
        name = action["action"]
        if name not in FIELDS:
            raise ValueError(f"Unsupported action: {name}.")
        _fields(action, *FIELDS[name])
        item = action["item"]
        _text(item)
        if name in ("set_goal", "set_limit"):
            table = "goals" if name == "set_goal" else "limits"
            if item not in before[table]:
                before[table][item] = targets.load(conn, table, item)
            after[table][item] = targets.validate(action, today)
        else:
            _session(conn, action, today, before, after)
            after["items"][item]["why"] = proposal["why"]
            after["items"][item]["sources"] = copy.deepcopy(proposal["sources"])
    for state in after["items"].values():
        state["version"] += 1
        state["plan_version"] = proposal["plan_version"] + 1
    return before, after


def _undo_items(conn: sqlite3.Connection, prior_items: dict, target: int) -> tuple[dict, dict]:
    before, after = {}, {}
    for item, prior in prior_items.items():
        current = _load(conn, item)
        if current is None or current["status"] in ("sent", "unknown"):
            raise ValueError("This item needs delivery reconciliation before undo.")
        before[item] = current
        restored = copy.deepcopy(prior if prior is not None else current)
        if prior is None:
            restored["status"] = "removed"
        if restored["status"] in ("sent", "unknown"):
            raise ValueError("This item needs delivery reconciliation before undo.")
        restored["version"] = current["version"] + 1
        restored["plan_version"] = target + 1
        after[item] = restored
    return before, after


def prepare_undo(conn: sqlite3.Connection, target: int) -> tuple[dict, dict]:
    """Restore the latest local change without overwriting later saved choices."""
    if type(target) is not int or target < 1 or target != journal.version(conn):
        raise ValueError("The undo is stale. Read the current plan again.")
    row = conn.execute("SELECT * FROM journal WHERE version = ?", (target,)).fetchone()
    original = json.loads(row["proposal_json"])
    allowed = FIELDS.keys() - ({"set_goal", "set_limit"} if row["format_version"] == 1 else set())
    supported = set(original) == {"undo_version"} or (
        "actions" in original and all(action["action"] in allowed for action in original["actions"])
    )
    if row["format_version"] not in (1, 2) or not supported:
        raise ValueError("This saved operation does not support local undo.")
    prior = json.loads(row["before_json"])
    if row["format_version"] == 1:
        prior = {"items": prior, "goals": {}, "limits": {}}
    before, after = {}, copy.deepcopy(prior)
    before["items"], after["items"] = _undo_items(conn, prior["items"], target)
    for table in ("goals", "limits"):
        before[table] = {item: targets.load(conn, table, item) for item in prior[table]}
    return before, after
