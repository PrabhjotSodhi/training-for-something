"""Validate supported proposal actions and calculate their resulting user state."""

import copy
import json
import sqlite3
from datetime import date

from tfs.proposals import journal, matching, targets
from tfs.proposals.models import Proposal


def _day(value: str, today: date) -> str:
    if date.fromisoformat(value) < today:
        raise ValueError("A planned date must be today or later, in YYYY-MM-DD form.")
    return value


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
    Proposal.model_validate(proposal)
    for names in proposal.get("delivery_targets", {}).values():
        if names != sorted(set(names)):
            raise ValueError("Delivery targets must be sorted without duplicates.")
    if proposal["plan_version"] != journal.version(conn):
        raise ValueError("The proposal is stale. Read the current plan and propose again.")


def _forecast(
    conn: sqlite3.Connection, action: dict, today: date, sources: list, goals: dict
) -> dict:
    item = action["item"]
    goal = goals[item] if item in goals else targets.load(conn, "goals", item)
    if goal is None:
        raise ValueError("Unknown goal.")
    return {
        "goal_id": item,
        "low": action["low"],
        "high": action["high"],
        "unit": action["unit"],
        "method": action["method"],
        "sources": copy.deepcopy(sources),
        "local_date": today.isoformat(),
    }


def _add(conn: sqlite3.Connection, action: dict, today: date, pending_goals: dict) -> dict:
    goals = action.get("goal_ids", [])
    for goal in goals:
        if (
            goal not in pending_goals
            and conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal,)).fetchone() is None
        ):
            raise ValueError("Unknown goal.")
    return {
        "id": action["item"],
        "version": 0,
        "local_date": _day(action["date"], today),
        "start_hint": action.get("start_hint"),
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
    if name in ("move_session", "change_session"):
        state["status"] = "saved"
    if "start_hint" in action:
        state["start_hint"] = action["start_hint"]
    if name == "move_session":
        state["local_date"] = _day(action["to"], today)
    elif name == "change_session":
        if "label" in action:
            state["label"] = action["label"]
        if "parts" in action:
            state["parts"] = copy.deepcopy(action["parts"])
    else:
        state["status"] = "removed" if name == "remove_session" else "done"


def _restore(conn: sqlite3.Connection, state: dict | None, action: dict, today: date) -> None:
    if state is None or state["status"] != "removed":
        raise ValueError("Only a removed plan item can be restored.")
    state["local_date"] = _day(action["to"], today)
    if "start_hint" in action:
        state["start_hint"] = action["start_hint"]
    state["status"] = "saved"


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
        if action["action"] != "mark_done" and matching.completed(
            conn, item, after.get("exclusions", {}).get(item, ())
        ):
            raise ValueError("This plan item is done according to an imported workout.")
        if action["action"] == "restore_session":
            _restore(conn, after["items"][item], action, today)
        else:
            _edit(after["items"][item], action, today)


def _unlink(conn: sqlite3.Connection, action: dict, before: dict, after: dict) -> None:
    item, records = action["item"], action["record_ids"]
    if records != sorted(set(records)) or records != matching.matched_records(conn, item):
        raise ValueError("The workout match changed. Read it and preview again.")
    before.setdefault("exclusions", {}).setdefault(item, matching.exclusions(conn, item))
    after.setdefault("exclusions", {})[item] = sorted(
        set(before["exclusions"][item]) | set(records)
    )


def prepare(conn: sqlite3.Connection, proposal: dict, today: date) -> tuple[dict, dict]:
    """Validate a batch in order, returning original and resulting user states."""
    _proposal(conn, proposal)
    before = {"items": {}, "goals": {}, "limits": {}}
    after = copy.deepcopy(before)
    for action in proposal["actions"]:
        name, item = action["action"], action["item"]
        if name in ("set_goal", "set_limit"):
            table = "goals" if name == "set_goal" else "limits"
            if item not in before[table]:
                before[table][item] = targets.load(conn, table, item)
            after[table][item] = targets.validate(action, today)
        elif name == "set_forecast":
            if item not in before.setdefault("forecasts", {}):
                row = conn.execute(
                    "SELECT * FROM coach_forecasts WHERE goal_id = ?", (item,)
                ).fetchone()
                before["forecasts"][item] = row and {
                    **dict(row),
                    "sources": json.loads(row["sources_json"]),
                }
            after.setdefault("forecasts", {})[item] = _forecast(
                conn, action, today, proposal["sources"], after["goals"]
            )
        elif name == "unlink_plan_match":
            _unlink(conn, action, before, after)
        else:
            _session(conn, action, today, before, after)
            after["items"][item]["why"] = proposal["why"]
            after["items"][item]["sources"] = copy.deepcopy(proposal["sources"])
    for state in after["items"].values():
        state["version"] += 1
        state["plan_version"] = proposal["plan_version"] + 1
    return before, after
