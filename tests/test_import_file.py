import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tfs import config
from tfs.cli import main
from tfs.connectors.core.contract import load_manifests
from tfs.importer.session import (
    AmbiguousRouteError,
    NoReaderError,
    import_file,
    route_file,
)
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

NOW = "2026-10-27T09:00:00Z"
EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_NZ">
 <ExportDate value="2026-03-02 09:00:00 +1300"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Ana's iPhone" unit="count" creationDate="2026-03-01 07:20:00 +1300" startDate="2026-03-01 07:12:33 +1300" endDate="2026-03-01 07:20:00 +1300" value="532"/>
 <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" sourceName="Ana's iPhone" unit="kcal" creationDate="2026-03-01 07:20:00 +1300" startDate="2026-03-01 07:12:33 +1300" endDate="2026-03-01 07:20:00 +1300" value="100"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Ana's iPhone" creationDate="2026-03-01 08:30:00 +1300" startDate="2026-03-01 08:00:00 +1300" endDate="2026-03-01 08:30:00 +1300"/>
</HealthData>
"""


class ImportTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.dict(os.environ, {"TFS_HOME": folder.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.export = Path(folder.name) / "inbox" / "export.xml"
        self.export.parent.mkdir()
        self.export.write_text(EXPORT, encoding="utf-8")

    def count(self, sql: str) -> int:
        conn = connect(config.db_path(), "read_only")
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["import", *argv])
        return code, out.getvalue(), err.getvalue()


class ImportFileTest(ImportTest):
    def setUp(self) -> None:
        super().setUp()
        config.data_dir().mkdir(parents=True, exist_ok=True)
        self.conn = connect(config.db_path())
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def test_an_export_is_saved_read_and_stored(self) -> None:
        result = import_file(self.conn, config.data_dir(), self.export, now=NOW)

        self.assertEqual(
            (result.connector, result.status, result.skipped), ("apple_health", "ok", False)
        )
        self.assertEqual(self.conn.execute("SELECT status FROM files").fetchone()[0], "ok")
        self.assertEqual(self.count("SELECT COUNT(*) FROM source_records"), 3)

    def test_importing_the_same_file_again_does_not_read_it(self) -> None:
        import_file(self.conn, config.data_dir(), self.export, now=NOW)

        with mock.patch("tfs.connectors.core.runner.run") as run:
            again = import_file(self.conn, config.data_dir(), self.export, now=NOW)
            code, out, err = self.run_cli(str(self.export))

        run.assert_not_called()
        self.assertTrue(again.skipped)
        self.assertEqual((code, out, err), (0, "export.xml was already imported.\n", ""))
        self.assertEqual(
            (
                self.count("SELECT COUNT(*) FROM files"),
                self.count("SELECT COUNT(*) FROM source_records"),
            ),
            (1, 3),
        )

    def test_a_file_read_by_an_older_reader_is_read_again(self) -> None:
        import_file(self.conn, config.data_dir(), self.export, now=NOW)
        self.conn.execute("UPDATE files SET reader_version = 'older'")

        again = import_file(self.conn, config.data_dir(), self.export, now=NOW)

        self.assertEqual((again.status, again.skipped, again.inserted), ("ok", False, 3))
        self.assertEqual(self.count("SELECT COUNT(*) FROM source_records"), 3)
        self.assertNotEqual(
            self.conn.execute("SELECT reader_version FROM files").fetchone()[0], "older"
        )


class RouteFileTest(unittest.TestCase):
    def test_a_name_with_no_matching_connector_is_refused(self) -> None:
        with self.assertRaisesRegex(NoReaderError, r"^No connector reads notes\.txt\.$"):
            route_file("notes.txt", load_manifests())

    def test_a_name_that_two_connectors_read_is_refused_naming_both(self) -> None:
        manifests = {
            "first_app": {"routes": {"files": ["*.zip"]}},
            "second_app": {"routes": {"files": ["EXPORT.*"]}},
        }

        with self.assertRaisesRegex(AmbiguousRouteError, "first_app.*second_app"):
            route_file("export.zip", manifests)

    def test_the_match_ignores_case(self) -> None:
        self.assertEqual(
            route_file("Export.ZIP", load_manifests()), ("apple_health", "file", "zip")
        )


class ImportCommandTest(ImportTest):
    def test_an_export_is_imported_and_the_counts_are_printed(self) -> None:
        code, out, err = self.run_cli(str(self.export))

        self.assertEqual(
            (code, out, err),
            (0, "Imported export.xml with apple_health: 3 records stored, 0 rejected (ok).\n", ""),
        )

    def test_a_file_no_connector_reads_exits_2(self) -> None:
        notes = self.export.with_name("notes.txt")
        notes.write_text("hello", encoding="utf-8")

        code, out, err = self.run_cli(str(notes))

        self.assertEqual((code, out, err), (2, "", "No connector reads notes.txt.\n"))

    def test_a_connector_name_that_does_not_exist_exits_2_and_saves_nothing(self) -> None:
        for name in ("applehealth", "hevy"):
            with self.subTest(name=name):
                code, out, err = self.run_cli(str(self.export), "--connector", name)

                self.assertEqual(
                    (code, out, err), (2, "", f"No connector named {name} reads files.\n")
                )
                self.assertEqual(self.count("SELECT COUNT(*) FROM files"), 0)

    def test_a_truncated_export_prints_the_reader_message_and_exits_1(self) -> None:
        self.export.write_text(EXPORT[:300], encoding="utf-8")

        code, out, err = self.run_cli(str(self.export))

        self.assertEqual(
            (code, out, err),
            (
                1,
                "",
                "The export is incomplete or damaged: unclosed token: line 4, column 1.\n",
            ),
        )

    def test_a_missing_file_exits_1(self) -> None:
        missing = self.export.parent / "gone" / "export.xml"

        code, out, err = self.run_cli(str(missing))

        self.assertEqual((code, out, err), (1, "", f"{missing.resolve()} does not exist.\n"))
