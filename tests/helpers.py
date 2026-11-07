import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from tfs.importer.records import store
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

FIRST_FILE_SHA256 = f"{1:064x}"


def open_database(case: unittest.TestCase, role: str = "default"):
    """Open a migrated database in a temporary folder that the test removes."""
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    conn = connect(Path(folder.name) / "tfs.db", role)
    case.addCleanup(conn.close)
    migrate(conn)
    return conn


def insert_file(conn, arrival_seq: int, received_at: str, connector: str = "hevy") -> str:
    """Add a file row if it is missing and return its sha256."""
    sha256 = f"{arrival_seq:064x}"
    conn.execute(
        "INSERT OR IGNORE INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
        " received_at) VALUES (?, ?, ?, 'json', 'json', 1, ?)",
        (sha256, arrival_seq, connector, received_at),
    )
    return sha256


def store_file(conn, records, arrival_seq: int, received_at: str, reader_version="r1", **options):
    """Create the file row for an arrival and store its records."""
    return store(
        conn,
        connector="testapp",
        file_sha256=insert_file(conn, arrival_seq, received_at, "testapp"),
        arrival_seq=arrival_seq,
        received_at=received_at,
        reader_version=reader_version,
        records=records,
        **options,
    )


SOURCE_RECORD_COLUMNS = (
    "id connector stream key kind revision_at arrival_seq file_sha256 reader_version start_utc"
    " end_utc local_date label family metric values_json origin_app origin_device deleted_at"
).split()
FIRST_FILE = {
    "revision_at": "2026-10-01T00:00:00.000Z",
    "arrival_seq": 1,
    "file_sha256": FIRST_FILE_SHA256,
    "reader_version": "r1",
}


def _insert_source_record(conn, row: dict) -> None:
    unknown = row.keys() - set(SOURCE_RECORD_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown source_records columns: {sorted(unknown)}")
    conn.execute(
        "INSERT INTO source_records (id, connector, stream, key, kind, revision_at, arrival_seq,"
        " file_sha256, reader_version, start_utc, end_utc, local_date, label, family, metric,"
        " values_json, origin_app, origin_device, deleted_at)"
        " VALUES (:id, :connector, :stream, :key, :kind, :revision_at, :arrival_seq,"
        " :file_sha256, :reader_version, :start_utc, :end_utc, :local_date, :label, :family,"
        " :metric, :values_json, :origin_app, :origin_device, :deleted_at)",
        dict.fromkeys(SOURCE_RECORD_COLUMNS) | row,
    )


def insert_workout(
    conn,
    record_id: str,
    connector: str,
    start: str = "2026-10-01T06:00:00Z",
    end: str | None = "2026-10-01T07:00:00Z",
    values: dict | None = None,
    **columns,
) -> None:
    """Add a workout row from the first file. Keywords set other source_records columns."""
    row = {
        "id": record_id,
        "key": record_id,
        "connector": connector,
        "stream": "workouts",
        "kind": "session",
        "start_utc": start,
        "end_utc": end,
        "local_date": start[:10],
        "family": "strength",
        "values_json": json.dumps(values or {}),
    }
    _insert_source_record(conn, FIRST_FILE | row | columns)


def insert_measurement(
    conn, key: str, connector: str, metric: str, start_utc: str, values: dict, **columns
) -> None:
    """Add a measurement row from the first file. Keywords set other source_records columns."""
    row = {
        "id": key,
        "key": key,
        "connector": connector,
        "stream": metric,
        "kind": "measurement",
        "start_utc": start_utc,
        "local_date": start_utc[:10],
        "metric": metric,
        "values_json": json.dumps(values),
    }
    _insert_source_record(conn, FIRST_FILE | row | columns)


def read_records(
    case: unittest.TestCase, read: Callable, path: str | Path, fmt: str
) -> tuple[dict, list[dict]]:
    """Read a file through a connector and return its result and decoded records."""
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    out_path = Path(folder.name) / "records.jsonl"
    result = read(str(path), fmt, str(out_path))
    if not out_path.exists():
        return result, []
    lines = out_path.read_text(encoding="utf-8").splitlines()
    return result, [json.loads(line) for line in lines]
