"""Preview and atomically save local plan, goal and limit proposals."""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from tfs.connectors.core.contract import to_utc_text
from tfs.persistence.sqlite import connect, save_intent, transaction
from tfs.proposals import actions, journal, targets
from tfs.scheduling import jobs


def preview(db_path: Path, proposal: dict, *, now: datetime) -> dict:
    """Return validated resulting items without changing stored state."""
    to_utc_text(now)
    journal.encode(proposal)
    with closing(connect(db_path, "read_only")) as conn:
        conn.execute("BEGIN")
        _, items = actions.prepare(conn, proposal, now.date())
        delivery, labels = jobs.destinations(conn, items["items"])
    return {
        "plan_version": proposal["plan_version"],
        "delivery_targets": delivery,
        "delivery_apps": labels,
        **{name: list(states.values()) for name, states in items.items()},
    }


def _write(conn: sqlite3.Connection, state: dict, version: int) -> None:
    row = {
        **state,
        "plan_version": version,
        "goal_ids_json": journal.encode(state["goal_ids"]),
        "sources_json": journal.encode(state["sources"]),
    }
    conn.execute(
        "INSERT INTO plan_items"
        " (id, version, plan_version, local_date, start_hint, family, label,"
        " goal_ids_json, why, sources_json, status)"
        " VALUES (:id, :version, :plan_version, :local_date, :start_hint, :family, :label,"
        " :goal_ids_json, :why, :sources_json, :status)"
        " ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
        " plan_version=excluded.plan_version, local_date=excluded.local_date,"
        " start_hint=excluded.start_hint, label=excluded.label, why=excluded.why, sources_json=excluded.sources_json,"
        " status=excluded.status",
        row,
    )
    conn.execute("DELETE FROM session_parts WHERE plan_item_id = ?", (state["id"],))
    for seq, part in enumerate(state["parts"], 1):
        conn.execute(
            "INSERT INTO session_parts (plan_item_id, seq, part_kind, label,"
            " duration_s, distance_m, reps, weight_kg, avg_hr, extras_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state["id"],
                seq,
                part["part_kind"],
                part.get("label"),
                part.get("duration_s"),
                part.get("distance_m"),
                part.get("reps"),
                part.get("weight_kg"),
                part.get("avg_hr"),
                part.get("extras_json"),
            ),
        )


def _save(
    db_path: Path,
    proposal: dict,
    *,
    request_id: str,
    approved_by: str,
    now: datetime,
    restoring: bool = False,
) -> dict:
    saved_at = to_utc_text(now)
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("A save needs a request ID.")
    if approved_by not in ("chat", "screen"):
        raise ValueError("Unknown approval route.")
    if not db_path.exists():
        raise ValueError("TFS has no data yet. Run tfs import or tfs start first.")
    encoded = journal.encode(proposal)
    with save_intent(db_path), closing(connect(db_path, "save")) as conn, transaction(conn):
        previous = journal.receipt(conn, request_id, encoded)
        if previous is not None:
            return previous
        if restoring:
            before, items = actions.prepare_undo(conn, proposal["undo_version"])
        else:
            before, items = actions.prepare(conn, proposal, now.date())
        if restoring:
            prior_jobs = items.pop("jobs", {})
            job_before, job_after = jobs.restore(conn, items["items"], prior_jobs)
        else:
            delivery, _ = jobs.destinations(conn, items["items"])
            if proposal.get("delivery_targets", {}) != delivery:
                raise ValueError(
                    "Delivery targets changed or need approval. Preview again before Save."
                )
            job_before, job_after = jobs.prepare(conn, items["items"], delivery, saved_at)
        if job_before:
            before["jobs"] = job_before
        version = journal.append(conn, request_id, encoded, before, approved_by, saved_at)
        targets.write(conn, items)
        for state in items["items"].values():
            _write(conn, state, version)
        jobs.write(conn, job_after, version, saved_at)
    return {"request_id": request_id, "plan_version": version}


def save(
    db_path: Path,
    proposal: dict,
    *,
    request_id: str,
    approved_by: str,
    now: datetime,
) -> dict:
    """Apply an approved batch once, or roll back every change on failure."""
    return _save(db_path, proposal, request_id=request_id, approved_by=approved_by, now=now)


def undo(
    db_path: Path,
    target_version: int,
    *,
    request_id: str,
    approved_by: str,
    now: datetime,
) -> dict:
    """Undo the latest local save in a new, retry-safe journal transaction."""
    return _save(
        db_path,
        {"undo_version": target_version},
        request_id=request_id,
        approved_by=approved_by,
        now=now,
        restoring=True,
    )
