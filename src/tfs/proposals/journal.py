"""The append-only receipt for each approved local Save."""

import json
import sqlite3

FORMAT_VERSION = 5


def encode(value: object) -> str:
    """Encode finite JSON consistently so a retry can compare its content."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Proposal values must be finite JSON.") from error


def version(conn: sqlite3.Connection) -> int:
    """Return the latest saved-state version, or zero before the first Save."""
    return conn.execute("SELECT coalesce(max(version), 0) FROM journal").fetchone()[0]


def receipt(conn: sqlite3.Connection, request_id: str, proposal_json: str) -> dict | None:
    """Return an existing matching receipt, refusing reuse for different content."""
    row = conn.execute(
        "SELECT version, proposal_json FROM journal WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    if row["proposal_json"] != proposal_json:
        raise ValueError("This request ID already saved different content.")
    return {"request_id": request_id, "plan_version": row["version"]}


def append(
    conn: sqlite3.Connection,
    request_id: str,
    proposal_json: str,
    before: dict,
    approved_by: str,
    saved_at: str,
) -> int:
    """Append a receipt inside the caller's transaction and return its version."""
    return conn.execute(
        "INSERT INTO journal (request_id, proposal_json, before_json, approved_by, saved_at, format_version)"
        " VALUES (?, ?, ?, ?, ?, ?) RETURNING version",
        (
            request_id,
            proposal_json,
            encode(before),
            approved_by,
            saved_at,
            FORMAT_VERSION,
        ),
    ).fetchone()[0]
