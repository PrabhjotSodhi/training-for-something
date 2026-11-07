"""Queue exact approved deliveries in the caller's Save or undo transaction."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from tfs.connectors.core import send
from tfs.connectors.core.contract import load_manifests, parse_iso_time, to_utc_text
from tfs.persistence.sqlite import connect, transaction
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


def recover(conn: sqlite3.Connection) -> None:
    """After exclusive engine startup, require reconciliation of interrupted sends."""
    conn.execute("UPDATE jobs SET status = 'in_doubt' WHERE status = 'sending'")


def _claim(conn: sqlite3.Connection, now: str, manifests: dict) -> dict | None:
    with transaction(conn):
        for row in conn.execute(
            "SELECT j.*, r.remote_id, r.remote_version FROM jobs j"
            " JOIN sources s ON s.connector = j.connector AND s.enabled = 1"
            " LEFT JOIN remote_links r ON r.item_id = j.item_id AND r.connector = j.connector"
            " WHERE j.status IN ('pending', 'not_sent', 'in_doubt') AND j.next_attempt_at <= ?"
            " ORDER BY j.created_at, j.id",
            (now,),
        ):
            item = json.loads(row["payload_json"])
            manifest = manifests.get(row["connector"])
            if manifest is None or item["family"] not in manifest["sends"]:
                continue
            job = {**dict(row), "item": item, "in_doubt": row["status"] == "in_doubt"}
            conn.execute(
                "UPDATE jobs SET status = 'sending', attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            return job
    return None


def _finish(conn: sqlite3.Connection, job: dict, outcome: dict, now: str) -> None:
    status = outcome["status"]
    due = to_utc_text(parse_iso_time(now) + timedelta(minutes=5)) if status == "not_sent" else now
    with transaction(conn):
        changed = conn.execute(
            "UPDATE jobs SET status = ?, next_attempt_at = ?, result_json = ?"
            " WHERE id = ? AND status = 'sending'",
            (status, due, encode(outcome), job["id"]),
        ).rowcount
        if changed != 1:
            raise ValueError("The delivery job is no longer sending.")
        if status == "sent":
            conn.execute(
                "INSERT INTO remote_links(item_id, connector, remote_id, remote_version, last_job_id)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(item_id, connector) DO UPDATE SET"
                " remote_id=excluded.remote_id, remote_version=excluded.remote_version, last_job_id=excluded.last_job_id",
                (
                    job["item_id"],
                    job["connector"],
                    outcome["remote_id"],
                    outcome["remote_version"],
                    job["id"],
                ),
            )
            conn.execute(
                "UPDATE plan_items SET status = 'sent' WHERE id = ? AND status = 'saved'"
                " AND NOT EXISTS (SELECT 1 FROM jobs WHERE item_id = ? AND status NOT IN ('sent', 'cancelled'))",
                (job["item_id"], job["item_id"]),
            )


def run_one(db_path: Path, now: str) -> bool:
    """Claim and finish one due delivery; return false when no eligible work is due."""
    manifests = load_manifests()
    with closing(connect(db_path, "save")) as conn:
        job = _claim(conn, now, manifests)
    if job is None:
        return False
    outcome = send.deliver(job, manifests[job["connector"]], now)
    with closing(connect(db_path, "save")) as conn:
        _finish(conn, job, outcome, now)
    return True


def resume_auth(conn: sqlite3.Connection, connector: str, now: str) -> None:
    """Resume authentication-blocked work through reconciliation after reconnecting."""
    conn.execute(
        "UPDATE jobs SET status = 'in_doubt', next_attempt_at = ?"
        " WHERE connector = ? AND status = 'needs_you'"
        " AND json_extract(result_json, '$.auth_failed') = 1",
        (now, connector),
    )
