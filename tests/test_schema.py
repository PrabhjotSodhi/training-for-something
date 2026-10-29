import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

TABLES = {
    "files",
    "sources",
    "source_records",
    "source_deletes",
    "session_parts",
    "session_groups",
    "session_group_members",
    "day_facts",
    "rebuild_dates",
}

COLUMNS = {
    "files": [
        ("sha256", "TEXT"),
        ("arrival_seq", "INTEGER"),
        ("connector", "TEXT"),
        ("format", "TEXT"),
        ("ext", "TEXT"),
        ("size_bytes", "INTEGER"),
        ("received_at", "TEXT"),
        ("reader_version", "TEXT"),
        ("status", "TEXT"),
        ("message", "TEXT"),
    ],
    "sources": [
        ("connector", "TEXT"),
        ("enabled", "INTEGER"),
        ("cursor_json", "TEXT"),
        ("last_attempt_at", "TEXT"),
        ("last_contact_at", "TEXT"),
        ("last_received_at", "TEXT"),
        ("last_error", "TEXT"),
    ],
    "source_records": [
        ("id", "TEXT"),
        ("connector", "TEXT"),
        ("stream", "TEXT"),
        ("key", "TEXT"),
        ("kind", "TEXT"),
        ("revision_at", "TEXT"),
        ("arrival_seq", "INTEGER"),
        ("file_sha256", "TEXT"),
        ("reader_version", "TEXT"),
        ("start_utc", "TEXT"),
        ("end_utc", "TEXT"),
        ("utc_offset_min", "INTEGER"),
        ("local_date", "TEXT"),
        ("label", "TEXT"),
        ("family", "TEXT"),
        ("metric", "TEXT"),
        ("values_json", "TEXT"),
        ("origin_app", "TEXT"),
        ("origin_device", "TEXT"),
        ("plan_ref", "TEXT"),
        ("deleted_at", "TEXT"),
    ],
    "source_deletes": [
        ("connector", "TEXT"),
        ("stream", "TEXT"),
        ("key", "TEXT"),
        ("revision_at", "TEXT"),
        ("arrival_seq", "INTEGER"),
    ],
    "session_parts": [
        ("record_id", "TEXT"),
        ("seq", "INTEGER"),
        ("part_kind", "TEXT"),
        ("label", "TEXT"),
        ("duration_s", "REAL"),
        ("distance_m", "REAL"),
        ("reps", "INTEGER"),
        ("weight_kg", "REAL"),
        ("avg_hr", "REAL"),
        ("extras_json", "TEXT"),
    ],
    "session_groups": [
        ("id", "TEXT"),
        ("local_date", "TEXT"),
        ("family", "TEXT"),
        ("start_utc", "TEXT"),
        ("chosen_json", "TEXT"),
    ],
    "session_group_members": [("group_id", "TEXT"), ("record_id", "TEXT")],
    "day_facts": [
        ("local_date", "TEXT"),
        ("metric", "TEXT"),
        ("value", "REAL"),
        ("unit", "TEXT"),
        ("source", "TEXT"),
        ("coverage", "TEXT"),
        ("formula_version", "TEXT"),
    ],
    "rebuild_dates": [("local_date", "TEXT")],
}

PRIMARY_KEYS = {
    "files": ["sha256"],
    "sources": ["connector"],
    "source_records": ["id"],
    "source_deletes": ["connector", "stream", "key"],
    "session_parts": ["record_id", "seq"],
    "session_groups": ["id"],
    "session_group_members": ["group_id", "record_id"],
    "day_facts": ["local_date", "metric"],
    "rebuild_dates": ["local_date"],
}

INDEXES = {
    "source_records_day": ["local_date", "kind"],
    "source_records_metric_day": ["metric", "local_date"],
    "source_records_file": ["file_sha256"],
}

FILE = {
    "sha256": "a" * 64,
    "arrival_seq": 1,
    "connector": "hevy",
    "format": "json",
    "ext": "json",
    "size_bytes": 120,
    "received_at": "2026-10-20T19:06:12Z",
    "status": "new",
}

RECORD = {
    "id": "r1",
    "connector": "hevy",
    "stream": "workouts",
    "key": "w1",
    "kind": "session",
    "arrival_seq": 1,
    "file_sha256": FILE["sha256"],
    "reader_version": "abc123",
    "start_utc": "2026-10-20T18:00:00Z",
    "local_date": "2026-10-21",
    "values_json": "{}",
}

DELETE = {"connector": "hevy", "stream": "workouts", "key": "w1", "arrival_seq": 2}
PART = {"record_id": "r1", "seq": 0, "part_kind": "set"}
GROUP = {
    "id": "g1",
    "local_date": "2026-10-21",
    "start_utc": "2026-10-20T18:00:00Z",
    "chosen_json": "{}",
}
MEMBER = {"group_id": "g1", "record_id": "r1"}
FACT = {
    "local_date": "2026-10-21",
    "metric": "steps",
    "value": 9000.0,
    "unit": "count",
    "source": "hevy",
    "coverage": "full",
    "formula_version": "1",
}
SOURCE = {"connector": "hevy", "enabled": 1}
REBUILD = {"local_date": "2026-10-21"}

NOT_NULL = {
    "files": (
        {**FILE, "sha256": "e" * 64, "arrival_seq": 5},
        ["arrival_seq", "connector", "format", "ext", "size_bytes", "received_at", "status"],
    ),
    "sources": (SOURCE, ["enabled"]),
    "source_records": (
        {**RECORD, "id": "r9", "key": "w9"},
        [
            "connector",
            "stream",
            "key",
            "kind",
            "arrival_seq",
            "file_sha256",
            "reader_version",
            "start_utc",
            "local_date",
            "values_json",
        ],
    ),
    "source_deletes": (DELETE, ["connector", "stream", "key", "arrival_seq"]),
    "session_parts": (PART, ["record_id", "seq", "part_kind"]),
    "session_groups": (
        {**GROUP, "id": "g9"},
        ["local_date", "start_utc", "chosen_json"],
    ),
    "session_group_members": (MEMBER, ["group_id", "record_id"]),
    "day_facts": (
        FACT,
        ["local_date", "metric", "value", "unit", "source", "coverage", "formula_version"],
    ),
}


INSERTS = {
    "files": """INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,
        received_at, reader_version, status, message) VALUES (:sha256, :arrival_seq, :connector,
        :format, :ext, :size_bytes, :received_at, :reader_version, :status, :message)""",
    "sources": """INSERT INTO sources (connector, enabled, cursor_json, last_attempt_at,
        last_contact_at, last_received_at, last_error) VALUES (:connector, :enabled, :cursor_json,
        :last_attempt_at, :last_contact_at, :last_received_at, :last_error)""",
    "source_records": """INSERT INTO source_records (id, connector, stream, key, kind, revision_at,
        arrival_seq, file_sha256, reader_version, start_utc, end_utc, utc_offset_min, local_date,
        label, family, metric, values_json, origin_app, origin_device, plan_ref, deleted_at)
        VALUES (:id, :connector, :stream, :key, :kind, :revision_at, :arrival_seq, :file_sha256,
        :reader_version, :start_utc, :end_utc, :utc_offset_min, :local_date, :label, :family,
        :metric, :values_json, :origin_app, :origin_device, :plan_ref, :deleted_at)""",
    "source_deletes": """INSERT INTO source_deletes (connector, stream, key, revision_at,
        arrival_seq) VALUES (:connector, :stream, :key, :revision_at, :arrival_seq)""",
    "session_parts": """INSERT INTO session_parts (record_id, seq, part_kind, label, duration_s,
        distance_m, reps, weight_kg, avg_hr, extras_json) VALUES (:record_id, :seq, :part_kind,
        :label, :duration_s, :distance_m, :reps, :weight_kg, :avg_hr, :extras_json)""",
    "session_groups": """INSERT INTO session_groups (id, local_date, family, start_utc,
        chosen_json) VALUES (:id, :local_date, :family, :start_utc, :chosen_json)""",
    "session_group_members": """INSERT INTO session_group_members (group_id, record_id)
        VALUES (:group_id, :record_id)""",
    "day_facts": """INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,
        formula_version) VALUES (:local_date, :metric, :value, :unit, :source, :coverage,
        :formula_version)""",
    "rebuild_dates": "INSERT INTO rebuild_dates (local_date) VALUES (:local_date)",
}

COUNTS = """SELECT
    (SELECT COUNT(*) FROM files) AS files,
    (SELECT COUNT(*) FROM sources) AS sources,
    (SELECT COUNT(*) FROM source_records) AS source_records,
    (SELECT COUNT(*) FROM source_deletes) AS source_deletes,
    (SELECT COUNT(*) FROM session_parts) AS session_parts,
    (SELECT COUNT(*) FROM session_groups) AS session_groups,
    (SELECT COUNT(*) FROM session_group_members) AS session_group_members,
    (SELECT COUNT(*) FROM day_facts) AS day_facts,
    (SELECT COUNT(*) FROM rebuild_dates) AS rebuild_dates"""


def insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    """Insert one row. Columns the row leaves out are stored as NULL."""
    conn.execute(INSERTS[table], {**dict.fromkeys(dict(COLUMNS[table]).keys()), **row})


def count(conn: sqlite3.Connection, table: str) -> int:
    """Return how many rows a table holds."""
    return conn.execute(COUNTS).fetchone()[table]


class SchemaTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = connect(Path(folder.name) / "tfs.db")
        self.addCleanup(self.conn.close)
        self.assertEqual(migrate(self.conn), 1)

    def add_file(self, **changes) -> None:
        insert(self.conn, "files", {**FILE, **changes})

    def add_record(self, **changes) -> None:
        insert(self.conn, "source_records", {**RECORD, **changes})

    def add_session_with_part_and_group(self) -> None:
        self.add_file()
        self.add_record()
        insert(self.conn, "session_parts", PART)
        insert(self.conn, "session_groups", GROUP)
        insert(self.conn, "session_group_members", MEMBER)

    def test_a_new_database_gets_exactly_the_0_1_tables_at_schema_one(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        self.assertEqual({row["name"] for row in rows}, TABLES)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_running_migrate_again_keeps_the_data(self):
        self.add_file()
        self.assertEqual(migrate(self.conn), 1)
        self.assertEqual(count(self.conn, "files"), 1)

    def test_each_table_has_its_columns_in_order_with_their_types_and_keys(self):
        for table, columns in COLUMNS.items():
            with self.subTest(table=table):
                info = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
                self.assertEqual([(row["name"], row["type"]) for row in info], columns)
                keyed = sorted((row for row in info if row["pk"]), key=lambda row: row["pk"])
                self.assertEqual([row["name"] for row in keyed], PRIMARY_KEYS[table])

    def test_the_three_source_record_indexes_cover_the_right_columns(self):
        listed = self.conn.execute("PRAGMA index_list(source_records)").fetchall()
        named = {row["name"] for row in listed if row["origin"] == "c"}
        self.assertEqual(named, set(INDEXES))
        for name, columns in INDEXES.items():
            with self.subTest(index=name):
                info = self.conn.execute(f"PRAGMA index_info({name})").fetchall()
                self.assertEqual([row["name"] for row in info], columns)

    def test_a_record_must_name_a_file_that_exists(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_record()
        self.assertEqual(count(self.conn, "source_records"), 0)

    def test_a_file_a_record_came_from_cannot_be_deleted(self):
        self.add_file()
        self.add_record()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM files")
        self.assertEqual(count(self.conn, "files"), 1)

    def test_a_record_key_is_unique_within_its_connector_and_stream(self):
        self.add_file()
        self.add_record()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_record(id="r2")

    def test_the_same_key_in_another_stream_or_connector_is_allowed(self):
        self.add_file()
        self.add_record()
        self.add_record(id="r2", stream="templates")
        self.add_record(id="r3", connector="strava")
        self.assertEqual(count(self.conn, "source_records"), 3)

    def test_two_records_cannot_share_an_id(self):
        self.add_file()
        self.add_record()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_record(key="w2")

    def test_two_files_cannot_share_a_hash_or_an_arrival_number(self):
        self.add_file()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_file(arrival_seq=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_file(sha256="b" * 64)

    def test_a_file_status_must_be_one_of_the_eight(self):
        for number, status in enumerate(
            ["new", "reading", "ok", "partial", "unreadable", "needs_ai", "failed", "missing"]
        ):
            with self.subTest(status=status):
                self.add_file(sha256=f"{number:064d}", arrival_seq=number + 10, status=status)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_file(sha256="f" * 64, arrival_seq=99, status="bogus")

    def test_a_file_starts_as_new(self):
        self.conn.execute(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes, received_at)"
            " VALUES ('a', 1, 'hevy', 'json', 'json', 1, '2026-10-20T19:06:12Z')"
        )
        self.assertEqual(self.conn.execute("SELECT status FROM files").fetchone()[0], "new")

    def test_a_record_kind_must_be_one_of_the_four(self):
        self.add_file()
        for number, kind in enumerate(["session", "measurement", "event", "document"]):
            with self.subTest(kind=kind):
                self.add_record(id=f"k{number}", key=f"k{number}", kind=kind)
        for kind in ("deleted", "bogus"):
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                self.add_record(id="bad", key="bad", kind=kind)

    def test_a_day_fact_coverage_must_be_full_or_partial(self):
        insert(self.conn, "day_facts", FACT)
        insert(self.conn, "day_facts", {**FACT, "metric": "weight", "coverage": "partial"})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "day_facts", {**FACT, "metric": "sleep_asleep", "coverage": "none"})

    def test_a_day_has_one_fact_per_metric(self):
        insert(self.conn, "day_facts", FACT)
        insert(self.conn, "day_facts", {**FACT, "metric": "weight"})
        insert(self.conn, "day_facts", {**FACT, "local_date": "2026-10-22"})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "day_facts", {**FACT, "value": 1.0})

    def test_a_day_with_no_fact_has_no_row(self):
        self.assertEqual(count(self.conn, "day_facts"), 0)

    def test_required_columns_refuse_null(self):
        self.add_file()
        self.add_record()
        insert(self.conn, "session_groups", GROUP)
        for table, (row, columns) in NOT_NULL.items():
            self.conn.execute("SAVEPOINT probe")
            insert(self.conn, table, row)
            self.conn.execute("ROLLBACK TO probe")
            for column in columns:
                with (
                    self.subTest(table=table, column=column),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    insert(self.conn, table, {**row, column: None})
            self.conn.execute("RELEASE probe")

    def test_a_delete_notice_is_unique_per_connector_stream_and_key(self):
        insert(self.conn, "source_deletes", DELETE)
        insert(self.conn, "source_deletes", {**DELETE, "key": "w2"})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "source_deletes", {**DELETE, "arrival_seq": 3})

    def test_a_delete_notice_needs_no_record_or_file(self):
        insert(self.conn, "source_deletes", DELETE)
        self.assertEqual(count(self.conn, "source_deletes"), 1)

    def test_a_part_must_belong_to_a_record_that_exists(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_parts", PART)

    def test_a_part_number_is_unique_within_its_record(self):
        self.add_file()
        self.add_record()
        insert(self.conn, "session_parts", PART)
        insert(self.conn, "session_parts", {**PART, "seq": 1})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_parts", PART)

    def test_deleting_a_record_deletes_its_parts_and_group_memberships(self):
        self.add_session_with_part_and_group()
        self.conn.execute("DELETE FROM source_records")
        self.assertEqual(count(self.conn, "session_parts"), 0)
        self.assertEqual(count(self.conn, "session_group_members"), 0)
        self.assertEqual(count(self.conn, "session_groups"), 1)

    def test_deleting_a_group_deletes_its_members_but_not_their_records(self):
        self.add_session_with_part_and_group()
        self.conn.execute("DELETE FROM session_groups WHERE id = 'g1'")
        self.assertEqual(count(self.conn, "session_group_members"), 0)
        self.assertEqual(count(self.conn, "source_records"), 1)
        self.assertEqual(count(self.conn, "session_parts"), 1)

    def test_a_member_must_name_a_group_and_a_record_that_exist(self):
        self.add_file()
        self.add_record()
        insert(self.conn, "session_groups", GROUP)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", {**MEMBER, "group_id": "nope"})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", {**MEMBER, "record_id": "nope"})

    def test_a_record_belongs_to_at_most_one_group(self):
        self.add_session_with_part_and_group()
        insert(self.conn, "session_groups", {**GROUP, "id": "g2"})
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", {**MEMBER, "group_id": "g2"})

    def test_a_group_can_have_many_members(self):
        self.add_session_with_part_and_group()
        self.add_record(id="r2", key="w2")
        insert(self.conn, "session_group_members", {**MEMBER, "record_id": "r2"})
        self.assertEqual(count(self.conn, "session_group_members"), 2)

    def test_a_group_id_is_unique(self):
        insert(self.conn, "session_groups", GROUP)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_groups", GROUP)

    def test_a_date_waits_for_a_rebuild_only_once(self):
        insert(self.conn, "rebuild_dates", REBUILD)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "rebuild_dates", REBUILD)

    def test_a_source_has_one_row_per_connector(self):
        insert(self.conn, "sources", SOURCE)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "sources", SOURCE)

    def test_a_source_starts_enabled(self):
        self.conn.execute("INSERT INTO sources (connector) VALUES ('hevy')")
        self.assertEqual(self.conn.execute("SELECT enabled FROM sources").fetchone()[0], 1)

    def test_a_failed_insert_in_a_transaction_leaves_nothing_behind(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.add_file()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_record(file_sha256="missing")
        self.conn.execute("ROLLBACK")
        self.assertEqual(count(self.conn, "files"), 0)

    def test_the_tables_survive_closing_and_reopening(self):
        path = Path(self.conn.execute("PRAGMA database_list").fetchone()["file"])
        self.add_file()
        with closing(connect(path)) as again:
            self.assertEqual(migrate(again), 1)
            self.assertEqual(count(again, "files"), 1)


if __name__ == "__main__":
    unittest.main()
