"""Validate supported proposal actions and calculate their resulting user state."""

import copy
import json
import sqlite3
from datetime import date

from tfs.proposals import journal, matching, targets
from tfs.proposals.models import Proposal
from tfs.scheduling import jobs


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
    if conn.execute("SELECT 1 FROM remote_links WHERE item_id=?", (state["id"],)).fetchone():
        raise ValueError("Resolve the existing deletion task before restoring a new copy.")
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


def _report(conn: sqlite3.Connection, action: dict, count: int) -> dict:
    if count != 1:
        raise ValueError("Report a deletion separately before proposing another change.")
    return {action["op_id"]: jobs.prepare_report(conn, action)}


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
        elif name == "unlink_plan_match":
            _unlink(conn, action, before, after)
        elif name == "report_delivery_deleted":
            after["reports"] = _report(conn, action, len(proposal["actions"]))
        else:
            _session(conn, action, today, before, after)
            after["items"][item]["why"] = proposal["why"]
            after["items"][item]["sources"] = copy.deepcopy(proposal["sources"])
    for state in after["items"].values():
        state["version"] += 1
        state["plan_version"] = proposal["plan_version"] + 1
    return before, after


def _undo_items(
    conn: sqlite3.Connection, prior_items: dict, target: int, completion: set[str]
) -> tuple[dict, dict]:
    before, after = {}, {}
    for item, prior in prior_items.items():
        current = _load(conn, item)
        if current is None or current["status"] == "unknown":
            raise ValueError("This item needs delivery reconciliation before undo.")
        before[item] = current
        restored = copy.deepcopy(prior if prior is not None else current)
        if prior is None:
            restored["status"] = "removed"
        linked = conn.execute("SELECT 1 FROM remote_links WHERE item_id=?", (item,)).fetchone()
        if linked:
            allowed = {"saved", "sent", "removed"} | ({"done"} if item in completion else set())
            if current["status"] not in allowed or restored["status"] not in allowed:
                raise ValueError("This completion change needs reconciliation before undo.")
            if restored["status"] != "removed" and "done" not in (
                current["status"],
                restored["status"],
            ):
                restored["status"] = "saved"
        elif current["status"] == "sent" or restored["status"] in ("sent", "unknown"):
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
    if any(
        action.get("action") == "report_delivery_deleted" for action in original.get("actions", [])
    ):
        raise ValueError(
            "A reported external deletion cannot be undone. Propose restore_session to create a new copy."
        )
    not_yet = ("set_goal", "set_limit") if row["format_version"] == 1 else ()
    supported = ({"undo_version"} <= set(original) <= {"undo_version", "delivery_targets"}) or (
        "actions" in original
        and not any(action["action"] in not_yet for action in original["actions"])
    )
    if row["format_version"] not in (1, 2, 3, 4, 5) or not supported:
        raise ValueError("This saved operation does not support local undo.")
    prior = json.loads(row["before_json"])
    if row["format_version"] == 1:
        prior = {"items": prior, "goals": {}, "limits": {}}
    before, after = {}, copy.deepcopy(prior)
    completion = (
        set(prior["items"])
        if "undo_version" in original
        else {
            action["item"]
            for action in original.get("actions", [])
            if action["action"] == "mark_done"
        }
    )
    before["items"], after["items"] = _undo_items(conn, prior["items"], target, completion)
    after["completion_items"] = [
        item
        for item in sorted(completion)
        if "done" in (before["items"][item]["status"], after["items"][item]["status"])
    ]
    if "exclusions" in prior:
        before["exclusions"] = {
            item: matching.exclusions(conn, item) for item in prior["exclusions"]
        }
    for table in ("goals", "limits"):
        before[table] = {item: targets.load(conn, table, item) for item in prior[table]}
    return before, after
