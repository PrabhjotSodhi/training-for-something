import sqlite3
import tempfile
import unittest
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

INDEXES = {
    "source_records_day": ["local_date", "kind"],
    "source_records_metric_day": ["metric", "local_date"],
    "source_records_file": ["file_sha256"],
}

# Invented values for the columns a row cannot do without. Every column here is NOT NULL.
# Columns that have a database default (files.status, sources.enabled) are left out on purpose.
DEFAULTS = {
    "files": {
        "sha256": "a",
        "arrival_seq": 1,
        "connector": "hevy",
        "format": "json",
        "ext": "json",
        "size_bytes": 120,
        "received_at": "2026-10-20T19:06:12Z",
    },
    "sources": {"connector": "hevy"},
    "source_records": {
        "id": "r1",
        "connector": "hevy",
        "stream": "workouts",
        "key": "w1",
        "kind": "session",
        "revision_at": "2026-10-20T19:06:12.000Z",
        "arrival_seq": 1,
        "file_sha256": "a",
        "reader_version": "abc123",
        "start_utc": "2026-10-20T18:00:00Z",
        "local_date": "2026-10-21",
        "values_json": "{}",
    },
    "source_deletes": {
        "connector": "hevy",
        "stream": "workouts",
        "key": "w1",
        "revision_at": "2026-10-20T19:06:13.000Z",
        "arrival_seq": 2,
    },
    "session_parts": {"record_id": "r1", "seq": 0, "part_kind": "set"},
    "session_groups": {
        "id": "g1",
        "local_date": "2026-10-21",
        "start_utc": "2026-10-20T18:00:00Z",
        "chosen_json": "{}",
    },
    "session_group_members": {"group_id": "g1", "record_id": "r1"},
    "day_facts": {
        "local_date": "2026-10-21",
        "metric": "steps",
        "value": 9000.0,
        "unit": "count",
        "source": "hevy",
        "coverage": "full",
        "formula_version": "1",
    },
    "rebuild_dates": {"local_date": "2026-10-21"},
}


INSERT = "INSERT INTO {} ({}) VALUES ({})"
COUNT = "SELECT COUNT(*) FROM {}"


def insert(conn: sqlite3.Connection, table: str, **values) -> None:
    """Insert one row: the table's invented defaults, changed by `values`."""
    row = {**DEFAULTS[table], **values}
    names = ", ".join(row)
    marks = ", ".join(f":{name}" for name in row)
    conn.execute(INSERT.format(table, names, marks), row)


def count(conn: sqlite3.Connection, table: str) -> int:
    """Return how many rows a table holds."""
    return conn.execute(COUNT.format(table)).fetchone()[0]


class SchemaTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = connect(Path(folder.name) / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def add_session_with_part_and_group(self) -> None:
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts")
        insert(self.conn, "session_groups")
        insert(self.conn, "session_group_members")

    def test_a_new_database_gets_exactly_the_0_1_tables_at_schema_one(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        self.assertEqual({row["name"] for row in rows}, TABLES)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_running_migrate_again_keeps_the_data(self):
        insert(self.conn, "files")
        migrate(self.conn)
        self.assertEqual(count(self.conn, "files"), 1)

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
            insert(self.conn, "source_records")

    def test_a_file_a_record_came_from_cannot_be_deleted(self):
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM files")

    def test_a_record_key_is_unique_within_its_connector_and_stream(self):
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "source_records", id="r2")

    def test_the_same_key_in_another_stream_or_connector_is_allowed(self):
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "source_records", id="r2", stream="templates")
        insert(self.conn, "source_records", id="r3", connector="strava")
        self.assertEqual(count(self.conn, "source_records"), 3)

    def test_two_records_cannot_share_an_id(self):
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "source_records", key="w2")

    def test_two_files_cannot_share_a_hash_or_an_arrival_number(self):
        insert(self.conn, "files")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "files", arrival_seq=2)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "files", sha256="b")

    def test_a_file_status_must_be_one_of_the_eight(self):
        for number, status in enumerate(
            ["new", "reading", "ok", "partial", "unreadable", "needs_ai", "failed", "missing"]
        ):
            with self.subTest(status=status):
                insert(self.conn, "files", sha256=status, arrival_seq=number, status=status)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "files", sha256="bogus", arrival_seq=99, status="bogus")

    def test_a_file_starts_as_new(self):
        insert(self.conn, "files")
        self.assertEqual(self.conn.execute("SELECT status FROM files").fetchone()[0], "new")

    def test_a_record_kind_must_be_one_of_the_four(self):
        insert(self.conn, "files")
        for kind in ["session", "measurement", "event", "document"]:
            with self.subTest(kind=kind):
                insert(self.conn, "source_records", id=kind, key=kind, kind=kind)
        for kind in ("deleted", "bogus"):
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                insert(self.conn, "source_records", id="bad", key="bad", kind=kind)

    def test_a_day_fact_coverage_must_be_full_or_partial(self):
        insert(self.conn, "day_facts")
        insert(self.conn, "day_facts", metric="weight", coverage="partial")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "day_facts", metric="sleep_asleep", coverage="none")

    def test_a_day_has_one_fact_per_metric(self):
        insert(self.conn, "day_facts")
        insert(self.conn, "day_facts", metric="weight")
        insert(self.conn, "day_facts", local_date="2026-10-22")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "day_facts", value=1.0)

    def test_required_columns_refuse_null(self):
        columns = [(table, name) for table, row in DEFAULTS.items() for name in row]
        columns += [("files", "status"), ("sources", "enabled")]
        for table, name in columns:
            with (
                self.subTest(table=table, column=name),
                self.assertRaisesRegex(
                    sqlite3.IntegrityError, f"NOT NULL constraint failed: {table}.{name}$"
                ),
            ):
                insert(self.conn, table, **{name: None})

    def test_a_delete_notice_is_unique_per_connector_stream_and_key(self):
        insert(self.conn, "source_deletes")
        insert(self.conn, "source_deletes", key="w2")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "source_deletes", arrival_seq=3)

    def test_a_delete_notice_needs_no_record_or_file(self):
        insert(self.conn, "source_deletes")
        self.assertEqual(count(self.conn, "source_deletes"), 1)

    def test_a_part_must_belong_to_a_record_that_exists(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_parts")

    def test_a_part_number_is_unique_within_its_record(self):
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts")
        insert(self.conn, "session_parts", seq=1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_parts")

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
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_groups")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", group_id="nope")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", record_id="nope")

    def test_a_record_belongs_to_at_most_one_group(self):
        self.add_session_with_part_and_group()
        insert(self.conn, "session_groups", id="g2")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_group_members", group_id="g2")

    def test_a_group_id_is_unique(self):
        insert(self.conn, "session_groups")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "session_groups")

    def test_a_date_waits_for_a_rebuild_only_once(self):
        insert(self.conn, "rebuild_dates")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "rebuild_dates")

    def test_a_source_has_one_row_per_connector(self):
        insert(self.conn, "sources")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, "sources")

    def test_a_source_starts_enabled(self):
        insert(self.conn, "sources")
        self.assertEqual(self.conn.execute("SELECT enabled FROM sources").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
