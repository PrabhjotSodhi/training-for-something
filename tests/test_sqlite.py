import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tfs.persistence.sqlite import connect, transaction


def pragma(conn: sqlite3.Connection, name: str):
    """Return the current value of one PRAGMA."""
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


class SqliteTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tfs.db"

    def test_default_connection_settings(self):
        with closing(connect(self.path)) as conn:
            self.assertEqual(pragma(conn, "journal_mode"), "wal")
            self.assertEqual(pragma(conn, "synchronous"), 2)
            self.assertEqual(pragma(conn, "foreign_keys"), 1)
            self.assertEqual(pragma(conn, "busy_timeout"), 5000)
            self.assertEqual(pragma(conn, "wal_autocheckpoint"), 1000)

    def test_import_role_uses_the_default_settings(self):
        with closing(connect(self.path, role="import")) as conn:
            self.assertEqual(pragma(conn, "wal_autocheckpoint"), 1000)

    def test_save_role_never_checkpoints(self):
        with closing(connect(self.path, role="save")) as conn:
            self.assertEqual(pragma(conn, "wal_autocheckpoint"), 0)
            self.assertEqual(pragma(conn, "synchronous"), 2)

    def test_save_role_keeps_the_write_log_when_the_last_connection_closes(self):
        with closing(connect(self.path, role="save")) as conn:
            conn.execute("CREATE TABLE t (a)")
            with transaction(conn):
                conn.executemany("INSERT INTO t VALUES (?)", [(number,) for number in range(50)])
        self.assertTrue(Path(f"{self.path}-wal").exists())
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT SUM(a) FROM t").fetchone()[0], 1225)

    def test_rows_are_read_by_column_name(self):
        with closing(connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT 5 AS five").fetchone()["five"], 5)

    def test_read_only_role_cannot_create_tables(self):
        connect(self.path).close()
        with closing(connect(self.path, role="read_only")) as conn:
            self.assertEqual(pragma(conn, "busy_timeout"), 5000)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE x (a)")

    def test_unknown_role_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            connect(self.path, role="admin")
        self.assertIn("admin", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_transaction_commits_on_success(self):
        with closing(connect(self.path)) as conn:
            conn.execute("CREATE TABLE t (a)")
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
            self.assertFalse(conn.in_transaction)

    def test_transaction_rolls_back_and_reraises(self):
        with closing(connect(self.path)) as conn:
            conn.execute("CREATE TABLE t (a)")
            with self.assertRaises(ValueError), transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise ValueError("boom")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)

    def test_transaction_reraises_the_error_when_sqlite_already_rolled_back(self):
        with closing(connect(self.path)) as conn:
            conn.execute("CREATE TABLE t (a)")
            conn.execute(
                "CREATE TRIGGER no_negative BEFORE INSERT ON t WHEN NEW.a < 0 "
                "BEGIN SELECT RAISE(ROLLBACK, 'negative'); END"
            )
            with self.assertRaises(sqlite3.IntegrityError) as caught, transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                conn.execute("INSERT INTO t VALUES (-1)")
            self.assertEqual(str(caught.exception), "negative")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)

    def test_transaction_rolls_back_when_commit_fails(self):
        with closing(connect(self.path)) as conn:
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE child (parent_id INTEGER "
                "REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
            )
            with self.assertRaises(sqlite3.IntegrityError) as caught, transaction(conn):
                conn.execute("INSERT INTO child VALUES (99)")
            self.assertEqual(str(caught.exception), "FOREIGN KEY constraint failed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)
            with transaction(conn):
                conn.execute("INSERT INTO parent VALUES (1)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parent").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
