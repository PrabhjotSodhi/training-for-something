import importlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest import mock

import tfs.persistence
from tfs.persistence.migrations import DatabaseTooNewError, migrate
from tfs.persistence.sqlite import connect

SRC = str(Path(__file__).resolve().parents[1] / "src")


def table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the names of the user tables."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def user_version(conn: sqlite3.Connection) -> int:
    """Return the schema number stored in the file."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


class MigrateTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "tfs.db"
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()

    def write(self, name: str, sql: str) -> None:
        (self.migrations / name).write_text(sql, encoding="utf-8")

    def open(self) -> sqlite3.Connection:
        conn = connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def assert_untouched(self, conn: sqlite3.Connection) -> None:
        self.assertEqual(user_version(conn), 0)
        self.assertEqual(table_names(conn), set())
        self.assertFalse(conn.in_transaction)

    def test_applies_a_file_to_a_new_database_and_a_second_run_changes_nothing(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        self.assertEqual(user_version(conn), 1)
        self.assertEqual(table_names(conn), {"a"})
        changes = conn.total_changes
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        self.assertEqual(user_version(conn), 1)
        self.assertEqual(conn.total_changes, changes)
        self.assertFalse(conn.in_transaction)

    def test_a_failing_file_leaves_no_trace_of_the_files_before_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER); CREATE TABLE broken (")
        conn = self.open()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.assert_untouched(conn)

    def test_a_failing_file_after_a_good_upgrade_keeps_the_old_version(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        migrate(conn, migrations_dir=self.migrations)
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER);")
        self.write("0003_c.sql", "INSERT INTO missing VALUES (1);")
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.assertEqual(user_version(conn), 1)
        self.assertEqual(table_names(conn), {"a"})
        self.assertFalse(conn.in_transaction)

    def test_refuses_a_database_made_by_a_newer_tfs(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(DatabaseTooNewError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertEqual(
            str(caught.exception),
            "This database was made by a newer version of TFS (schema 99). "
            "Install that version, or restore a backup.",
        )
        self.assertEqual(table_names(conn), set())
        self.assertEqual(user_version(conn), 99)

    def test_refuses_a_version_one_above_the_newest_file(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        conn.execute("PRAGMA user_version = 2")
        with self.assertRaises(DatabaseTooNewError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("(schema 2)", str(caught.exception))

    def test_refuses_a_negative_schema_number(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        conn.execute("PRAGMA user_version = -1")
        with self.assertRaises(sqlite3.DatabaseError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertEqual(
            str(caught.exception),
            "This database has an invalid schema number (-1). Restore a backup.",
        )
        self.assertEqual(table_names(conn), set())
        self.assertEqual(user_version(conn), -1)

    def test_a_missing_number_raises_value_error_and_applies_nothing(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("0003_c.sql", "CREATE TABLE c (z INTEGER);")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("2", str(caught.exception))
        self.assertIn("0002", str(caught.exception))
        self.assert_untouched(conn)

    def test_files_must_start_at_one(self):
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER);")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0001", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_repeated_number_raises_value_error(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("0001_b.sql", "CREATE TABLE b (y INTEGER);")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0001", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_number_zero_file_raises_value_error(self):
        self.write("0000_zero.sql", "CREATE TABLE z (x INTEGER);")
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0000_zero.sql", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_sql_file_without_a_four_digit_number_raises_value_error(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("2_b.sql", "CREATE TABLE b (y INTEGER);")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("2_b.sql", str(caught.exception))
        self.assert_untouched(conn)

    def test_files_that_are_not_sql_are_ignored(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("notes.txt", "not a migration")
        self.write("0002_b.sql.bak", "not a migration")
        (self.migrations / "__pycache__").mkdir()
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)

    def test_an_up_to_date_database_needs_no_write_access(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        migrate(self.open(), migrations_dir=self.migrations)
        reader = connect(self.path, role="read_only")
        self.addCleanup(reader.close)
        self.assertEqual(migrate(reader, migrations_dir=self.migrations), 1)

    def test_a_folder_with_no_files_returns_zero(self):
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 0)
        self.assert_untouched(conn)

    def test_files_run_in_number_order_and_only_those_above_the_stored_version(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        migrate(conn, migrations_dir=self.migrations)
        conn.execute("INSERT INTO a VALUES (1)")
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER); INSERT INTO b VALUES (2);")
        self.write("0003_c.sql", "INSERT INTO b SELECT y + 1 FROM b;")
        self.write("0004_d.sql", "INSERT INTO b SELECT y * 10 FROM b;")
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 4)
        self.assertEqual(user_version(conn), 4)
        values = [row[0] for row in conn.execute("SELECT y FROM b ORDER BY y")]
        self.assertEqual(values, [2, 3, 20, 30])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM a").fetchone()[0], 1)

    def test_a_file_that_ends_in_a_comment_does_not_swallow_the_next_file(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER); -- the first table")
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER)")
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 2)
        self.assertEqual(table_names(conn), {"a", "b"})

    def test_a_file_cannot_finish_the_statement_of_the_file_before_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER, y")
        self.write("0002_b.sql", " INTEGER);")
        conn = self.open()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.assert_untouched(conn)

    def test_a_file_can_use_a_savepoint(self):
        self.write(
            "0001_a.sql",
            "CREATE TABLE a (x INTEGER); SAVEPOINT s; INSERT INTO a VALUES (1); ROLLBACK TO s;",
        )
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM a").fetchone()[0], 0)
        self.assertFalse(conn.in_transaction)

    def test_a_file_cannot_set_a_different_version(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER); PRAGMA user_version = 7;")
        conn = self.open()
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        self.assertEqual(user_version(conn), 1)

    def test_an_unclosed_comment_that_hides_the_commit_is_rolled_back(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER); /* never closed")
        conn = self.open()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.assert_untouched(conn)

    def test_a_statement_that_cannot_run_in_a_transaction_rolls_back_the_files_before_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("0002_v.sql", "VACUUM;")
        conn = self.open()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.assert_untouched(conn)

    def test_the_connection_can_be_used_again_after_a_failure(self):
        self.write("0001_a.sql", "CREATE TABLE broken (")
        conn = self.open()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        conn.execute("CREATE TABLE later (x INTEGER)")
        self.assertIn("later", table_names(conn))

    def test_a_transaction_the_caller_left_open_is_refused_and_kept(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = self.open()
        conn.execute("CREATE TABLE mine (x INTEGER)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO mine VALUES (1)")
        with self.assertRaises(sqlite3.ProgrammingError):
            migrate(conn, migrations_dir=self.migrations)
        self.assertTrue(conn.in_transaction)
        conn.execute("ROLLBACK")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM mine").fetchone()[0], 0)
        self.assertEqual(table_names(conn), {"mine"})

    def test_a_missing_folder_raises_value_error(self):
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.root / "nowhere")
        self.assertIn("nowhere", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_file_that_is_not_text_raises_value_error_naming_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        (self.migrations / "0002_b.sql").write_bytes(b"CREATE TABLE b (y INTEGER); \xff\xfe")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0002_b.sql", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_file_with_a_null_character_raises_value_error_naming_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);\0")
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0001_a.sql", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_file_that_cannot_be_opened_raises_value_error_naming_it(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        (self.migrations / "0002_b.sql").mkdir()
        conn = self.open()
        with self.assertRaises(ValueError) as caught:
            migrate(conn, migrations_dir=self.migrations)
        self.assertIn("0002_b.sql", str(caught.exception))
        self.assert_untouched(conn)

    def test_a_closed_connection_raises_sqlite_error(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        conn = connect(self.path)
        conn.close()
        with self.assertRaises(sqlite3.Error):
            migrate(conn, migrations_dir=self.migrations)

    def test_without_a_folder_it_reads_the_tfs_sql_package(self):
        package_root = self.root / "package"
        (package_root / "sql").mkdir(parents=True)
        (package_root / "sql" / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "sql" / "0001_a.sql").write_text(
            "CREATE TABLE a (x INTEGER);", encoding="utf-8"
        )
        conn = self.open()
        # The import system caches what it finds, so put everything back afterwards.
        self.addCleanup(importlib.invalidate_caches)
        search_path = [str(package_root), *tfs.persistence.__path__]
        with (
            mock.patch.dict(sys.modules),
            mock.patch.dict(vars(tfs.persistence), {"__path__": search_path}),
        ):
            sys.modules.pop("tfs.persistence.sql", None)
            importlib.invalidate_caches()
            self.assertEqual(migrate(conn), 1)
        self.assertEqual(table_names(conn), {"a"})

    def test_a_missing_sql_package_raises_value_error(self):
        conn = self.open()
        with (
            mock.patch("importlib.resources.files", side_effect=ModuleNotFoundError("sql")),
            self.assertRaises(ValueError) as caught,
        ):
            migrate(conn)
        self.assertIn("reinstall", str(caught.exception).lower())
        self.assert_untouched(conn)

    def migrate_while_another_process_finishes_first(self) -> tuple[int, list[int]]:
        """Run migrate on one connection while a second connection migrates just before its lock."""
        slow = self.open()
        other = self.open()
        other_results = []

        def let_the_other_process_finish_first(statement: str) -> None:
            if statement.startswith("BEGIN") and not other_results:
                other_results.append(migrate(other, migrations_dir=self.migrations))

        slow.set_trace_callback(let_the_other_process_finish_first)
        result = migrate(slow, migrations_dir=self.migrations)
        slow.set_trace_callback(None)
        return result, other_results

    def test_two_processes_on_a_new_database_apply_each_file_once(self):
        self.write("0001_a.sql", "CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (10);")
        self.write("0002_b.sql", "UPDATE t SET n = n * 2;")
        result, other_results = self.migrate_while_another_process_finishes_first()
        self.assertEqual(result, 2)
        self.assertEqual(other_results, [2])
        conn = self.open()
        self.assertEqual(conn.execute("SELECT n FROM t").fetchall()[0][0], 20)
        self.assertEqual(user_version(conn), 2)

    def test_two_processes_on_an_older_database_apply_each_file_once(self):
        self.write("0001_a.sql", "CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (10);")
        migrate(self.open(), migrations_dir=self.migrations)
        self.write("0002_b.sql", "UPDATE t SET n = n * 2;")
        result, other_results = self.migrate_while_another_process_finishes_first()
        self.assertEqual(result, 2)
        self.assertEqual(other_results, [2])
        self.assertEqual(self.open().execute("SELECT n FROM t").fetchall()[0][0], 20)

    def test_a_file_that_reads_first_holds_the_write_lock_from_the_start(self):
        self.write("0001_a.sql", "SELECT COUNT(*) FROM sqlite_master; CREATE TABLE a (x INTEGER);")
        conn = self.open()
        other = self.open()
        other.execute("PRAGMA busy_timeout=0")
        outcome = []

        def write_from_another_connection(statement: str) -> None:
            if statement.strip().startswith("CREATE TABLE a") and not outcome:
                try:
                    other.execute("CREATE TABLE elsewhere (x INTEGER)")
                except sqlite3.OperationalError as error:
                    outcome.append(str(error))
                else:
                    outcome.append("written")

        conn.set_trace_callback(write_from_another_connection)
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 1)
        conn.set_trace_callback(None)
        self.assertEqual(outcome, ["database is locked"])
        self.assertEqual(table_names(conn), {"a"})

    def test_a_process_killed_part_way_leaves_the_old_version(self):
        self.write("0001_a.sql", "CREATE TABLE a (x INTEGER);")
        self.write("0002_b.sql", "CREATE TABLE b (y INTEGER); SELECT die(); CREATE TABLE c (z);")
        child = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from tfs.persistence.migrations import migrate\n"
            "from tfs.persistence.sqlite import connect\n"
            "conn = connect(Path(sys.argv[1]))\n"
            "conn.create_function('die', 0, lambda: os._exit(3))\n"
            "migrate(conn, migrations_dir=Path(sys.argv[2]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.path), str(self.migrations)],
            env={**os.environ, "PYTHONPATH": SRC},
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        conn = self.open()
        self.assert_untouched(conn)
        (self.migrations / "0002_b.sql").write_text("CREATE TABLE b (y INTEGER);")
        self.assertEqual(migrate(conn, migrations_dir=self.migrations), 2)
        self.assertEqual(table_names(conn), {"a", "b"})


class ShippedMigrationsTest(unittest.TestCase):
    def test_no_shipped_file_manages_the_transaction_or_the_version(self):
        owned = re.compile(r"(BEGIN|COMMIT|ROLLBACK|END|PRAGMA\s+user_version)\b", re.IGNORECASE)
        trigger = re.compile(r"CREATE\s+(TEMP\s+|TEMPORARY\s+)?TRIGGER\b", re.IGNORECASE)
        offenders = []
        for entry in resources.files("tfs.persistence.sql").iterdir():
            if not entry.name.endswith(".sql"):
                continue
            in_trigger = False
            for number, line in enumerate(entry.read_text(encoding="utf-8").splitlines(), 1):
                text = line.strip()
                if in_trigger:
                    in_trigger = not re.match(r"END\b", text, re.IGNORECASE)
                elif trigger.match(text):
                    in_trigger = not re.search(r"\bEND\s*;\s*$", text, re.IGNORECASE)
                elif owned.match(text):
                    offenders.append(f"{entry.name}:{number}: {text}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
