"""Links the same workout from different apps into one session group (HLD 7.1, rules 2 and 5)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tfs.connectors.core.contract import load_manifests, parse_iso_time

TIME_TOLERANCE_S = 180
NULL_FAMILY_TOLERANCE_S = 60
DURATION_RATIO = 0.9

_SESSIONS_TO_GROUP = """
WITH reach AS (
  SELECT m.group_id FROM session_group_members m
  JOIN source_records r ON r.id = m.record_id
  WHERE r.local_date IN (SELECT value FROM json_each(:days))
)
SELECT id, connector, start_utc, end_utc, local_date, family, origin_app, values_json
FROM source_records
WHERE kind = 'session' AND deleted_at IS NULL AND (
  local_date IN (SELECT value FROM json_each(:days))
  OR id IN (SELECT record_id FROM session_group_members WHERE group_id IN (SELECT group_id FROM reach))
)
"""
_DELETE_GROUPS = """
DELETE FROM session_groups WHERE id IN (
  SELECT m.group_id FROM session_group_members m
  JOIN source_records r ON r.id = m.record_id
  WHERE r.local_date IN (SELECT value FROM json_each(:days))
)
"""


@dataclass(frozen=True)
class _Session:
    id: str
    connector: str
    start_utc: str
    start: datetime
    end: datetime | None
    local_date: str
    family: str | None
    origin_app: str | None


def update(conn: sqlite3.Connection, dates: set[str], manifests: dict | None = None) -> int:
    """Regroup the sessions on dates and a day either side and return how many groups that made.

    Sessions that share a group with any of them are regrouped too. The caller owns the
    transaction. manifests=None reads the connector manifests.
    """
    manifests = load_manifests() if manifests is None else manifests
    days = json.dumps(sorted({_shift(day, step) for day in dates for step in (-1, 0, 1)}))
    rows = conn.execute(_SESSIONS_TO_GROUP, {"days": days}).fetchall()
    # Plain DELETE and INSERT: INSERT OR REPLACE would cascade away member rows.
    conn.execute(_DELETE_GROUPS, {"days": days})
    groups: list[list[_Session]] = []
    for session in sorted(map(_session, rows), key=lambda found: (found.start, found.id)):
        for members in groups:
            if all(_agree(session, member, manifests) for member in members):
                members.append(session)
                break
        else:
            groups.append([session])
    for members in groups:
        group_id = min(member.id for member in members)
        family = next((m.family for m in members if m.family is not None), None)
        conn.execute(
            "INSERT INTO session_groups (id, local_date, family, start_utc, chosen_json)"
            " VALUES (?, ?, ?, ?, '{}')",
            (group_id, members[0].local_date, family, members[0].start_utc),
        )
        conn.executemany(
            "INSERT INTO session_group_members (group_id, record_id) VALUES (?, ?)",
            [(group_id, member.id) for member in members],
        )
    return len(groups)


def _shift(day: str, step: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=step)).isoformat()


def _session(row: sqlite3.Row) -> _Session:
    start = parse_iso_time(row["start_utc"])
    duration_s = json.loads(row["values_json"]).get("duration_s")
    if row["end_utc"] is not None:
        end = parse_iso_time(row["end_utc"])
    else:
        end = None if duration_s is None else start + timedelta(seconds=duration_s)
    return _Session(
        row["id"],
        row["connector"],
        row["start_utc"],
        start,
        end,
        row["local_date"],
        row["family"],
        row["origin_app"],
    )


def _agree(first: _Session, second: _Session, manifests: dict) -> bool:
    if first.connector == second.connector or first.end is None or second.end is None:
        return False
    gap_s = max(
        abs((first.start - second.start).total_seconds()),
        abs((first.end - second.end).total_seconds()),
    )
    if gap_s > TIME_TOLERANCE_S:
        return False
    shorter, longer = sorted(
        ((first.end - first.start).total_seconds(), (second.end - second.start).total_seconds())
    )
    if shorter < DURATION_RATIO * longer:
        return False
    if first.family is not None and first.family == second.family:
        return True
    if _claims(first, second, manifests) or _claims(second, first, manifests):
        return True
    return (first.family is None or second.family is None) and gap_s <= NULL_FAMILY_TOLERANCE_S


def _claims(copy: _Session, original: _Session, manifests: dict) -> bool:
    """Whether the copy's origin app is one of the original connector's app ids (HLD 7.1, rule 2)."""
    return copy.origin_app in manifests.get(original.connector, {}).get("app_ids", [])
