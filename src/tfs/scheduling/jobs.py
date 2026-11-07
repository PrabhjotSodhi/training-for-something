"""Queue exact approved deliveries in the caller's Save or undo transaction."""

import sqlite3
import uuid

from tfs.connectors.core.contract import load_manifests
from tfs.proposals.journal import encode


def destinations(conn: sqlite3.Connection, items: dict) -> tuple[dict, dict]:
    """Return eligible connector IDs per item and their display labels."""
    manifests = load_manifests()
    enabled = {row[0] for row in conn.execute("SELECT connector FROM sources WHERE enabled = 1")}
    targets = {}
    for item, state in items.items():
        names = sorted(
            name
            for name in enabled
            if name in manifests
            and state["family"] in manifests[name]["sends"]
            and state["status"] in ("saved", "sent")
        )
        if names:
            targets[item] = names
    labels = {name: manifests[name]["label"] for names in targets.values() for name in names}
    return targets, labels


def _pending(conn: sqlite3.Connection, items: dict) -> dict:
    rows = {}
    for item in items:
        for row in conn.execute("SELECT * FROM jobs WHERE item_id = ?", (item,)):
            if row["status"] not in ("pending", "not_sent", "cancelled"):
                raise ValueError("This item needs delivery reconciliation before changing it.")
            if row["status"] != "cancelled":
                rows[row["id"]] = dict(row)
    return rows


def _state(row: dict) -> dict:
    return {"status": row["status"], "next_attempt_at": row["next_attempt_at"]}


def prepare(
    conn: sqlite3.Connection, items: dict, targets: dict, saved_at: str
) -> tuple[dict, dict]:
    """Prepare cancellation and new jobs, retaining prior states for undo."""
    pending = _pending(conn, items)
    before = {key: _state(row) for key, row in pending.items()}
    after = {key: {**_state(row), "status": "cancelled"} for key, row in pending.items()}
    for item, names in targets.items():
        for name in names:
            key = str(uuid.uuid4())
            before[key] = None
            after[key] = {
                "item_id": item,
                "connector": name,
                "payload_json": encode(items[item]),
                "status": "pending",
                "next_attempt_at": saved_at,
            }
    return before, after


def restore(conn: sqlite3.Connection, items: dict, prior: dict) -> tuple[dict, dict]:
    """Undo queue changes using original operation IDs and immutable payloads."""
    _pending(conn, items)
    before, after = {}, {}
    for key, state in prior.items():
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (key,)).fetchone()
        if row is None or row["item_id"] not in items:
            raise ValueError("The saved delivery history is missing or inconsistent.")
        before[key] = _state(row)
        after[key] = state or {**before[key], "status": "cancelled"}
    return before, after


def write(conn: sqlite3.Connection, changes: dict, version: int, saved_at: str) -> None:
    """Apply prepared queue changes inside the same transaction as the journal."""
    for key, state in changes.items():
        if "payload_json" in state:
            conn.execute(
                "INSERT INTO jobs(id, item_id, plan_version, connector, payload_json,"
                " status, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    state["item_id"],
                    version,
                    state["connector"],
                    state["payload_json"],
                    state["status"],
                    state["next_attempt_at"],
                    saved_at,
                ),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status = ?, next_attempt_at = ? WHERE id = ?",
                (state["status"], state["next_attempt_at"], key),
            )
