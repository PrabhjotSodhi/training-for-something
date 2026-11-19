import sqlite3
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from tests.test_schema import insert
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect


class PlansSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.conn = connect(self.root / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def journal(self, request: str = "save-1") -> int:
        return self.conn.execute(
            "INSERT INTO journal (request_id, proposal_json, before_json, approved_by, saved_at)"
            " VALUES (?, '{}', '{}', 'chat', '2026-11-07T10:00:00Z') RETURNING version",
            (request,),
        ).fetchone()[0]

    def plan(self, id: str = "p1") -> None:
        self.conn.execute(
            "INSERT INTO plan_items (id, version, plan_version, local_date, family, label, why)"
            " VALUES (?, 1, 1, '2026-11-08', 'strength', 'Push', 'Goal plan')",
            (id,),
        )

    def test_upgrade_preserves_imported_rows_and_part_values(self) -> None:
        old = self.root / "old"
        old.mkdir()
        initial = resources.files("tfs.persistence.sql").joinpath("0001_init.sql")
        (old / initial.name).write_text(initial.read_text(encoding="utf-8"), encoding="utf-8")
        conn = connect(self.root / "old.db")
        self.addCleanup(conn.close)
        migrate(conn, old)
        insert(conn, "files")
        insert(conn, "source_records")
        insert(conn, "session_parts", reps=8, weight_kg=30, extras_json='{"rest_s":90}')
        source = tuple(conn.execute("SELECT * FROM source_records").fetchone())
        part = tuple(conn.execute("SELECT * FROM session_parts").fetchone())
        self.assertEqual(migrate(conn), 10)
        self.assertEqual(tuple(conn.execute("SELECT * FROM source_records").fetchone()), source)
        upgraded = conn.execute("SELECT * FROM session_parts").fetchone()
        self.assertEqual(tuple(upgraded)[:-1], part)
        self.assertIsNone(upgraded["plan_item_id"])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_delivery_migration_preserves_existing_user_state(self) -> None:
        old = self.root / "plans_v2"
        old.mkdir()
        for name in ("0001_init.sql", "0002_plans.sql"):
            source = resources.files("tfs.persistence.sql").joinpath(name)
            (old / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        conn = connect(self.root / "plans_v2.db")
        self.addCleanup(conn.close)
        self.assertEqual(migrate(conn, old), 2)
        conn.executescript(
            "INSERT INTO journal(request_id, proposal_json, before_json, approved_by, saved_at)"
            " VALUES ('old-save', '{}', '{}', 'chat', '2026-11-07T10:00:00Z');"
            "INSERT INTO plan_items(id, version, plan_version, local_date, family, label, why)"
            " VALUES ('p1', 1, 1, '2026-11-08', 'run', 'Easy', 'Goal');"
            "INSERT INTO session_parts(plan_item_id, seq, part_kind, distance_m)"
            " VALUES ('p1', 1, 'lap', 6001);"
            "INSERT INTO goals VALUES ('g1', 'custom', '2027-01-01', '{}');"
            "INSERT INTO limits VALUES ('l1', 'injury', '{}');"
        )
        queries = (
            "SELECT * FROM journal",
            "SELECT * FROM plan_items",
            "SELECT * FROM session_parts",
            "SELECT * FROM goals",
            "SELECT * FROM limits",
        )
        before = [[tuple(row) for row in conn.execute(query)] for query in queries]
        self.assertEqual(migrate(conn), 10)
        self.assertEqual([[tuple(row) for row in conn.execute(query)] for query in queries], before)
        self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_parts_have_one_existing_owner_and_unique_sequence(self) -> None:
        self.journal()
        self.plan()
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts")
        self.conn.execute(
            "INSERT INTO session_parts (plan_item_id, seq, part_kind) VALUES ('p1', 0, 'set')"
        )
        for record, plan, seq in [
            (None, None, 1),
            ("r1", "p1", 1),
            ("missing", None, 1),
            (None, "missing", 1),
            ("r1", None, 0),
            (None, "p1", 0),
        ]:
            with (
                self.subTest(record=record, plan=plan, seq=seq),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.conn.execute(
                    "INSERT INTO session_parts (record_id, plan_item_id, seq, part_kind)"
                    " VALUES (?, ?, ?, 'set')",
                    (record, plan, seq),
                )

    def test_deleting_either_owner_keeps_the_other_owners_parts(self) -> None:
        self.journal()
        self.plan()
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts")
        self.conn.execute(
            "INSERT INTO session_parts (plan_item_id, seq, part_kind) VALUES ('p1', 0, 'set')"
        )
        self.conn.execute("DELETE FROM source_records")
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute("SELECT record_id, plan_item_id FROM session_parts")
            ],
            [(None, "p1")],
        )
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts")
        self.conn.execute("DELETE FROM plan_items")
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute("SELECT record_id, plan_item_id FROM session_parts")
            ],
            [("r1", None)],
        )

    def test_journal_is_append_only_with_unique_request_ids(self) -> None:
        self.assertEqual(self.journal(), 1)
        self.assertEqual(self.journal("save-2"), 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal()
        for sql in [
            "DELETE FROM journal",
            "UPDATE journal SET proposal_json = '[]'",
            "INSERT OR REPLACE INTO journal (request_id, proposal_json, before_json,"
            " approved_by, saved_at) VALUES ('save-1', '[]', '{}', 'chat', '2026-11-07T10:00:00Z')",
            "INSERT OR REPLACE INTO journal (version, request_id, proposal_json, before_json,"
            " approved_by, saved_at) VALUES (1, 'new-id', '[]', '{}', 'chat', '2026-11-07T10:00:00Z')",
        ]:
            with (
                self.subTest(sql=sql),
                self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"),
            ):
                self.conn.execute(sql)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 2)

    def test_user_state_uses_existing_versions_and_valid_json(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan()
        self.journal()
        self.plan()
        self.conn.execute(
            "INSERT INTO goals VALUES ('g1', 'powerlifting', '2027-03-01',"
            ' \'{"value":400,"unit":"kg"}\')'
        )
        self.conn.execute("INSERT INTO limits VALUES ('l1', 'available_days', '[1,3,5]')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE goals SET target_json = 'not JSON'")
        self.assertEqual(
            tuple(
                self.conn.execute("SELECT version, plan_version, status FROM plan_items").fetchone()
            ),
            (1, 1, "saved"),
        )
