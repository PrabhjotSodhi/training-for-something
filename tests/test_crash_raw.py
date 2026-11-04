import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import crash_child
from tfs.importer.raw_store import scan_raw
from tfs.importer.session import process_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

ROOT = Path(__file__).parent.parent
SNAPSHOT_QUERIES = {
    "source_records": "SELECT * FROM source_records ORDER BY id",
    "session_parts": "SELECT * FROM session_parts ORDER BY record_id, seq",
    "session_groups": "SELECT * FROM session_groups ORDER BY id",
    "day_facts": "SELECT * FROM day_facts ORDER BY local_date, metric",
    "sources": "SELECT connector, cursor_json FROM sources",
}


class CrashRawTest(unittest.TestCase):
    def new_folder(self) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def crash_at(self, point: str) -> Path:
        """Run the import in a child process that stops at the point, and return its folder."""
        folder = self.new_folder()
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        child = subprocess.run(
            [sys.executable, "-m", "tests.crash_child", point, str(folder)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((child.returncode, child.stderr), (1, ""))
        return folder

    def restart(self, folder: Path) -> None:
        """Start up as the engine does: register what is on disk, then read what is new."""
        with contextlib.closing(connect(folder / "tfs.db")) as conn:
            migrate(conn)
            scan_raw(conn, folder)
            process_pending(conn, folder, module=crash_child.MODULE)

    def uncrashed(self) -> dict:
        folder = self.new_folder()
        crash_child.operate(folder)
        return self.snapshot(folder)

    def snapshot(self, folder: Path) -> dict:
        """The stored data. revision_at is left out because recovery sets it from the file time."""
        with contextlib.closing(connect(folder / "tfs.db")) as conn:
            return {
                table: [
                    {name: value for name, value in dict(row).items() if name != "revision_at"}
                    for row in conn.execute(query)
                ]
                for table, query in SNAPSHOT_QUERIES.items()
            }

    def count(self, folder: Path, query: str) -> int:
        with contextlib.closing(connect(folder / "tfs.db")) as conn:
            return conn.execute(query).fetchone()[0]

    def raw_files(self, folder: Path) -> list[Path]:
        return [path for path in (folder / "raw").glob("*/*/*") if "tmp-" not in path.name]

    def test_a_crash_before_the_rename_leaves_nothing_and_the_file_can_be_saved_again(
        self,
    ) -> None:
        folder = self.crash_at("before_rename")

        self.restart(folder)

        self.assertEqual(self.count(folder, "SELECT count(*) FROM files"), 0)
        self.assertEqual(self.raw_files(folder), [])
        crash_child.operate(folder)
        self.assertEqual(self.snapshot(folder), self.uncrashed())

    def test_a_crash_after_the_rename_registers_the_file_once_with_the_same_records(
        self,
    ) -> None:
        folder = self.crash_at("after_rename")
        self.assertEqual(self.count(folder, "SELECT count(*) FROM files"), 0)

        self.restart(folder)

        self.assertEqual(self.count(folder, "SELECT count(*) FROM files"), 1)
        expected = self.uncrashed()
        found = self.snapshot(folder)
        # The cursor was in the transaction that never committed, so the next fetch sets it.
        for snapshot in (found, expected):
            snapshot["sources"] = [row["connector"] for row in snapshot["sources"]]
        self.assertEqual(found, expected)

    def test_a_crash_before_the_reader_starts_loses_nothing(self) -> None:
        folder = self.crash_at("before_read")
        self.assertEqual(self.count(folder, "SELECT count(*) FROM files"), 1)

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.uncrashed())

    def test_a_crash_after_the_first_batch_stores_no_record_twice(self) -> None:
        folder = self.crash_at("mid_read")
        self.assertEqual(
            self.count(folder, "SELECT count(*) FROM source_records"), crash_child.BATCH_SIZE
        )

        self.restart(folder)

        self.assertEqual(self.snapshot(folder), self.uncrashed())
