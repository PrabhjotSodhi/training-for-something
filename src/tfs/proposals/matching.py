"""Match completed workout groups without overwriting user-owned completion."""

import sqlite3
from collections.abc import Sequence
from datetime import timedelta, timezone, tzinfo

from tfs.connectors.core.contract import parse_iso_time

_CANDIDATES = """
WITH refs AS (
  SELECT DISTINCT l.item_id, m.group_id
  FROM remote_links l JOIN source_records r
    ON r.connector = l.connector AND r.plan_ref = l.remote_id AND r.deleted_at IS NULL
  JOIN session_group_members m ON m.record_id = r.id
), pairs AS (
  SELECT item_id, group_id, 0 AS fallback FROM refs
  UNION ALL
  SELECT p.id, g.id, 1 FROM plan_items p JOIN session_groups g
    ON p.local_date = g.local_date AND p.family = g.family
)
SELECT p.id AS item_id, g.id AS group_id, p.start_hint, r.start_utc,
  r.utc_offset_min, pairs.fallback
FROM pairs JOIN plan_items p ON p.id = pairs.item_id
JOIN session_groups g ON g.id = pairs.group_id
JOIN source_records r ON r.id = (
  SELECT s.id FROM session_group_members m JOIN source_records s ON s.id = m.record_id
  WHERE m.group_id = g.id AND s.deleted_at IS NULL ORDER BY s.start_utc, s.id LIMIT 1
)
WHERE p.status IN ('saved', 'sent', 'done', 'missed', 'unknown')
AND NOT EXISTS (
  SELECT 1 FROM plan_match_exclusions e JOIN session_group_members m ON m.record_id=e.record_id
  JOIN source_records s ON s.id=m.record_id AND s.deleted_at IS NULL
  WHERE e.item_id=p.id AND m.group_id=g.id
)
AND NOT EXISTS (
  SELECT 1 FROM session_group_members m JOIN source_records s ON s.id = m.record_id
  JOIN rebuild_dates d ON d.local_date = s.local_date WHERE m.group_id = g.id
)
"""


def _rank(row: sqlite3.Row, tz: tzinfo | None) -> tuple:
    start = parse_iso_time(row["start_utc"])
    offset = row["utc_offset_min"]
    local = start.astimezone(timezone(timedelta(minutes=offset)) if offset is not None else tz)
    hint = row["start_hint"]
    distance = 0
    if hint is not None:
        hour, minute = map(int, hint.split(":"))
        distance = abs(
            local.hour * 3600 + local.minute * 60 + local.second - hour * 3600 - minute * 60
        )
    return (row["fallback"], hint is None, distance, start, row["group_id"], row["item_id"])


def rebuild(conn: sqlite3.Connection, *, tz: tzinfo | None = None) -> None:
    """Reassign available workout groups deterministically in the caller's transaction."""
    candidates = sorted(conn.execute(_CANDIDATES), key=lambda row: _rank(row, tz))
    conn.execute("DELETE FROM plan_matches")
    items, groups = set(), set()
    for row in candidates:
        item, group = row["item_id"], row["group_id"]
        if item in items or group in groups:
            continue
        conn.execute(
            "INSERT INTO plan_matches VALUES (?, ?, ?)",
            (item, group, "date_family" if row["fallback"] else "reference"),
        )
        items.add(item)
        groups.add(group)


def matched_records(conn: sqlite3.Connection, item: str) -> list[str]:
    """Return the active source IDs of a match whose import rebuild is complete."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT r.id FROM plan_matches p JOIN session_group_members m ON m.group_id=p.group_id"
            " JOIN source_records r ON r.id=m.record_id WHERE p.item_id=? AND r.deleted_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM session_group_members gm JOIN source_records s ON s.id=gm.record_id"
            " JOIN rebuild_dates d ON d.local_date=s.local_date WHERE gm.group_id=p.group_id) ORDER BY r.id",
            (item,),
        )
    ]


def completed(conn: sqlite3.Connection, item: str, exclusions: Sequence[str] = ()) -> bool:
    """Whether active matched evidence remains after this proposal's exclusions."""
    records = matched_records(conn, item)
    return bool(records) and not set(records).intersection(exclusions)


def exclusions(conn: sqlite3.Connection, item: str) -> list[str]:
    """Read the user's stable source-record exclusions for one plan item."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT record_id FROM plan_match_exclusions WHERE item_id=? ORDER BY record_id",
            (item,),
        )
    ]


def write_exclusions(conn: sqlite3.Connection, changes: dict) -> None:
    """Restore the selected exclusions in the caller's journal transaction."""
    for item, records in changes.items():
        conn.execute("DELETE FROM plan_match_exclusions WHERE item_id=?", (item,))
        conn.executemany(
            "INSERT INTO plan_match_exclusions VALUES (?, ?)",
            [(item, record) for record in records],
        )
