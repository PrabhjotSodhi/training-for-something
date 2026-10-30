"""Stores what a reader returns: a new version of a record replaces the old one."""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import tzinfo
from itertools import batched

from tfs.connectors.core.contract import (
    RecordError,
    parse_iso_time,
    record_id,
    to_revision_text,
    validate_record,
)
from tfs.persistence.sqlite import transaction

REASON_LENGTH = 60

# Fields copied from the record as they are.
_RECORD_FIELDS = (
    "stream",
    "key",
    "kind",
    "start_utc",
    "end_utc",
    "utc_offset_min",
    "label",
    "family",
    "metric",
    "origin_app",
    "origin_device",
    "plan_ref",
)
_PART_FIELDS = (
    "seq",
    "part_kind",
    "label",
    "duration_s",
    "distance_m",
    "reps",
    "weight_kg",
    "avg_hr",
)

# deleted_at is left out so a newer version never clears a delete.
_UPSERT = """
INSERT INTO source_records (id, connector, stream, key, kind, revision_at, arrival_seq,
  file_sha256, reader_version, start_utc, end_utc, utc_offset_min, local_date, label, family,
  metric, values_json, origin_app, origin_device, plan_ref)
VALUES (:id, :connector, :stream, :key, :kind, :revision_at, :arrival_seq, :file_sha256,
  :reader_version, :start_utc, :end_utc, :utc_offset_min, :local_date, :label, :family,
  :metric, :values_json, :origin_app, :origin_device, :plan_ref)
ON CONFLICT (id) DO UPDATE SET
  kind = excluded.kind, revision_at = excluded.revision_at, arrival_seq = excluded.arrival_seq,
  file_sha256 = excluded.file_sha256, reader_version = excluded.reader_version,
  start_utc = excluded.start_utc, end_utc = excluded.end_utc,
  utc_offset_min = excluded.utc_offset_min, local_date = excluded.local_date,
  label = excluded.label, family = excluded.family, metric = excluded.metric,
  values_json = excluded.values_json, origin_app = excluded.origin_app,
  origin_device = excluded.origin_device, plan_ref = excluded.plan_ref
"""
_INSERT_PART = """
INSERT INTO session_parts (record_id, seq, part_kind, label, duration_s, distance_m, reps,
  weight_kg, avg_hr, extras_json)
VALUES (:record_id, :seq, :part_kind, :label, :duration_s, :distance_m, :reps, :weight_kg,
  :avg_hr, :extras_json)
"""


@dataclass
class StoreResult:
    """What one call to store did, with the reasons for rejected records and every local date touched."""

    inserted: int = 0
    replaced: int = 0
    unchanged: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    dates: set[str] = field(default_factory=set)


def store(
    conn: sqlite3.Connection,
    *,
    connector: str,
    file_sha256: str,
    arrival_seq: int,
    received_at: str,
    reader_version: str,
    records: Iterable[dict],
    tz: tzinfo | None = None,
    batch_size: int = 500,
    before_batch: Callable[[], None] | None = None,
) -> StoreResult:
    """Store a reader's records, commit every batch_size records and return what changed.

    A record with no local_date gets one from start_utc in tz, or in the computer's zone
    when tz is None. before_batch runs before each batch's transaction opens.
    """
    arrival_revision = to_revision_text(parse_iso_time(received_at))
    result = StoreResult()
    for batch in batched(records, batch_size):
        if before_batch is not None:
            before_batch()
        with transaction(conn):
            changed_dates: set[str] = set()
            for rec in batch:
                try:
                    _check(rec)
                except RecordError as error:
                    result.rejected[str(error)[:REASON_LENGTH]] += 1
                    continue
                _store_one(
                    conn,
                    rec,
                    connector=connector,
                    file_sha256=file_sha256,
                    arrival_seq=arrival_seq,
                    revision=rec.get("revision_at") or arrival_revision,
                    reader_version=reader_version,
                    tz=tz,
                    result=result,
                    changed_dates=changed_dates,
                )
            conn.executemany(
                "INSERT OR IGNORE INTO rebuild_dates (local_date) VALUES (?)",
                [(date,) for date in sorted(changed_dates)],
            )
            result.dates |= changed_dates
    return result


def _check(rec: dict) -> None:
    validate_record(rec)
    if rec["kind"] == "deleted":
        raise RecordError("deleted records are not stored yet")


def _store_one(
    conn: sqlite3.Connection,
    rec: dict,
    *,
    connector: str,
    file_sha256: str,
    arrival_seq: int,
    revision: str,
    reader_version: str,
    tz: tzinfo | None,
    result: StoreResult,
    changed_dates: set[str],
) -> None:
    rid = record_id(connector, rec["stream"], rec["key"])
    stored = conn.execute(
        "SELECT revision_at, arrival_seq, reader_version, local_date FROM source_records"
        " WHERE id = ?",
        (rid,),
    ).fetchone()
    if stored is not None:
        stored_order = (stored[0], stored[1])
        if (revision, arrival_seq) < stored_order or (
            (revision, arrival_seq) == stored_order and reader_version == stored[2]
        ):
            result.unchanged += 1
            return
        changed_dates.add(stored[3])
        # Updating in place keeps the group links, which INSERT OR REPLACE would cascade away.
        conn.execute("DELETE FROM session_parts WHERE record_id = ?", (rid,))
        result.replaced += 1
    else:
        result.inserted += 1
    local_date = rec.get("local_date") or (
        parse_iso_time(rec["start_utc"]).astimezone(tz).date().isoformat()
    )
    conn.execute(
        _UPSERT,
        {
            **{name: rec.get(name) for name in _RECORD_FIELDS},
            "id": rid,
            "connector": connector,
            "revision_at": revision,
            "arrival_seq": arrival_seq,
            "file_sha256": file_sha256,
            "reader_version": reader_version,
            "local_date": local_date,
            "values_json": json.dumps(rec.get("values") or {}, sort_keys=True),
        },
    )
    changed_dates.add(local_date)
    if rec["kind"] == "session":
        conn.executemany(_INSERT_PART, [_part_row(rid, part) for part in rec.get("parts", [])])


def _part_row(rid: str, part: dict) -> dict:
    extras = part.get("extras")
    return {
        **{name: part.get(name) for name in _PART_FIELDS},
        "record_id": rid,
        "extras_json": None if extras is None else json.dumps(extras, sort_keys=True),
    }
