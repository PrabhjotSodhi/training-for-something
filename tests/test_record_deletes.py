import tempfile
import unittest
from pathlib import Path

from tests.test_records import SESSION, changed
from tfs.importer.records import store
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

RECEIVED_AT = {seq: f"2026-10-01T{7 + seq:02d}:00:00.000Z" for seq in range(1, 7)}

DELETE = {"kind": "deleted", "stream": "workouts", "key": "w1"}


class DeleteTest(unittest.TestCase):
    def setUp(self):
        self.conn = self.open_database()

    def open_database(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        conn = connect(Path(folder.name) / "tfs.db", "import")
        self.addCleanup(conn.close)
        migrate(conn)
        for seq, received_at in RECEIVED_AT.items():
            conn.execute(
                "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
                " received_at) VALUES (?, ?, 'testapp', 'json', 'json', 1, ?)",
                (f"{seq:064x}", seq, received_at),
            )
        return conn

    def store(self, records, arrival, conn=None):
        return store(
            conn or self.conn,
            connector="testapp",
            file_sha256=f"{arrival:064x}",
            arrival_seq=arrival,
            received_at=RECEIVED_AT[arrival],
            reader_version="r1",
            records=records,
        )

    def row(self, key="w1", conn=None):
        found = (conn or self.conn).execute("SELECT * FROM source_records WHERE key = ?", (key,))
        return found.fetchone()

    def test_a_delete_hides_the_record_and_an_older_version_cannot_bring_it_back(self):
        self.store([SESSION], arrival=2)

        result = self.store([DELETE], arrival=3)

        self.assertEqual(result.deleted, 1)
        self.assertEqual(self.row()["deleted_at"], RECEIVED_AT[3])

        self.store([SESSION], arrival=2)

        self.assertEqual(self.row()["deleted_at"], RECEIVED_AT[3])

    def test_a_delete_that_arrives_first_hides_an_older_version_but_not_a_newer_one(self):
        key = "w-9"
        self.store([changed(DELETE, key=key)], arrival=5)
        self.assertIsNone(self.row(key))

        self.store([changed(SESSION, key=key)], arrival=4)

        self.assertEqual(self.row(key)["deleted_at"], RECEIVED_AT[5])

        self.store([changed(SESSION, key=key)], arrival=6)

        self.assertIsNone(self.row(key)["deleted_at"])

    def test_a_delete_with_no_revision_hides_a_session_that_has_one(self):
        self.store([changed(SESSION, revision_at="2026-10-01T08:30:00.000Z")], arrival=1)

        self.store([changed(DELETE, revision_at=None)], arrival=2)

        self.assertEqual(self.row()["deleted_at"], RECEIVED_AT[2])

    def test_a_delete_reports_the_local_date_of_the_record_it_hides(self):
        self.store([SESSION], arrival=1)
        self.conn.execute("DELETE FROM rebuild_dates")

        result = self.store([DELETE], arrival=2)

        self.assertEqual(result.dates, {"2026-10-02"})
        saved = self.conn.execute("SELECT local_date FROM rebuild_dates").fetchall()
        self.assertEqual([row[0] for row in saved], ["2026-10-02"])

    def test_the_row_is_the_same_whatever_order_the_content_and_delete_arrive_in(self):
        first = changed(SESSION, label="A", revision_at="2026-10-01T09:00:00.100Z")
        second = changed(SESSION, label="B", revision_at="2026-10-01T09:00:00.200Z")
        delete = changed(DELETE, revision_at="2026-10-01T09:00:00.300Z")
        orders = {
            "delete between": [(first, 1), (delete, 2), (second, 3)],
            "delete last": [(first, 1), (second, 2), (delete, 3)],
        }
        for name, steps in orders.items():
            conn = self.open_database()
            for rec, arrival in steps:
                self.store([rec], arrival=arrival, conn=conn)
            with self.subTest(name):
                row = self.row(conn=conn)
                self.assertEqual(
                    (row["label"], row["revision_at"], row["deleted_at"]),
                    ("B", "2026-10-01T09:00:00.200Z", "2026-10-01T09:00:00.300Z"),
                )

    def test_deleted_counts_only_rows_that_went_from_visible_to_hidden(self):
        self.store([SESSION, changed(SESSION, key="w2")], arrival=1)

        first = self.store([DELETE, changed(DELETE, key="w-unknown")], arrival=2)
        second = self.store([DELETE, changed(DELETE, key="w2")], arrival=3)

        self.assertEqual((first.deleted, second.deleted), (1, 1))
