import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from tfs.importer.raw_store import save_raw, scan_raw
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

NOW = "2026-10-27T09:00:00Z"
OLDER_MTIME = 1_790_000_000
NEWER_MTIME = 1_790_000_600


class ScanRawTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        self.conn = connect(self.data_dir / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def drop_file(self, data: bytes, mtime: int, connector="hevy", fmt="workouts_page") -> str:
        """Put a file under raw/ the way a crash leaves it: no files row."""
        sha256 = hashlib.sha256(data).hexdigest()
        folder = self.data_dir / "raw" / connector / fmt
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sha256}.json"
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return sha256

    def save(self, data: bytes) -> Path:
        return save_raw(
            self.conn, self.data_dir, "hevy", "workouts_page", "json", io.BytesIO(data), now=NOW
        ).path

    def row(self, sha256: str):
        return self.conn.execute("SELECT * FROM files WHERE sha256 = ?", (sha256,)).fetchone()

    def test_files_with_no_row_are_registered_oldest_first(self):
        newer = self.drop_file(b"newer", NEWER_MTIME)
        older = self.drop_file(b"older", OLDER_MTIME)

        result = scan_raw(self.conn, self.data_dir)

        self.assertEqual(result.registered, [older, newer])
        self.assertEqual(result.missing, [])
        first, second = self.row(older), self.row(newer)
        self.assertEqual((first["arrival_seq"], second["arrival_seq"]), (1, 2))
        self.assertEqual((first["status"], second["status"]), ("new", "new"))
        self.assertEqual(
            (first["connector"], first["format"], first["ext"], first["size_bytes"]),
            ("hevy", "workouts_page", "json", 5),
        )
        self.assertEqual(first["received_at"], "2026-09-21T14:13:20Z")

    def test_a_row_whose_file_was_removed_is_marked_missing(self):
        path = self.save(b"gone")
        sha256 = path.stem
        path.unlink()

        result = scan_raw(self.conn, self.data_dir)

        self.assertEqual((result.registered, result.missing), ([], [sha256]))
        row = self.row(sha256)
        self.assertEqual((row["status"], row["message"]), ("missing", "The raw file is missing."))

    def test_a_tmp_file_is_ignored_and_left_in_place(self):
        folder = self.data_dir / "raw" / "hevy" / "workouts_page"
        folder.mkdir(parents=True)
        temporary = folder / ".tmp-abc123"
        temporary.write_bytes(b"half written")

        result = scan_raw(self.conn, self.data_dir)

        self.assertEqual((result.registered, result.missing), ([], []))
        self.assertTrue(temporary.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_a_second_scan_registers_nothing_and_restored_or_reading_rows_become_new(self):
        orphan = self.drop_file(b"orphan", OLDER_MTIME)
        restored_path = self.save(b"restored")
        reading = restored_path.stem
        self.conn.execute("UPDATE files SET status = 'reading' WHERE sha256 = ?", (reading,))
        removed = self.save(b"removed")
        removed.unlink()
        scan_raw(self.conn, self.data_dir)
        removed.write_bytes(b"removed")

        again = scan_raw(self.conn, self.data_dir)

        self.assertEqual((again.registered, again.missing), ([], []))
        for sha256 in (orphan, reading, removed.stem):
            self.assertEqual(self.row(sha256)["status"], "new", sha256)
            self.assertIsNone(self.row(sha256)["message"], sha256)

    def test_a_file_that_is_still_gone_is_reported_on_every_scan(self):
        path = self.save(b"gone")
        path.unlink()
        scan_raw(self.conn, self.data_dir)

        again = scan_raw(self.conn, self.data_dir)

        self.assertEqual(again.missing, [path.stem])

    def test_a_finished_row_whose_file_is_present_is_left_alone(self):
        path = self.save(b"done")
        self.conn.execute("UPDATE files SET status = 'ok' WHERE sha256 = ?", (path.stem,))

        scan_raw(self.conn, self.data_dir)

        self.assertEqual(self.row(path.stem)["status"], "ok")

    def test_a_file_with_the_same_bytes_as_a_registered_one_is_not_registered_again(self):
        self.save(b"same")
        self.drop_file(b"same", OLDER_MTIME, connector="strava", fmt="activities")

        result = scan_raw(self.conn, self.data_dir)

        self.assertEqual(result.registered, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)

    def source_received_at(self):
        return self.conn.execute(
            "SELECT last_received_at FROM sources WHERE connector = 'hevy'"
        ).fetchone()[0]

    def test_a_source_with_no_receipt_time_takes_the_recovered_files_time(self):
        self.conn.execute("INSERT INTO sources (connector) VALUES ('hevy')")
        self.drop_file(b"orphan", OLDER_MTIME)

        scan_raw(self.conn, self.data_dir)

        self.assertEqual(self.source_received_at(), "2026-09-21T14:13:20Z")

    def test_a_source_keeps_the_later_of_its_time_and_the_recovered_files_time(self):
        self.conn.execute(
            "INSERT INTO sources (connector, cursor_json, last_contact_at, last_received_at)"
            " VALUES ('hevy', '{\"page\": 3}', '2026-10-01T00:00:00Z', '2026-09-01T00:00:00Z')"
        )
        self.drop_file(b"newer", NEWER_MTIME)

        scan_raw(self.conn, self.data_dir)

        self.assertEqual(self.source_received_at(), "2026-09-21T14:23:20Z")
        row = self.conn.execute("SELECT * FROM sources WHERE connector = 'hevy'").fetchone()
        self.assertEqual(
            (row["cursor_json"], row["last_contact_at"]),
            ('{"page": 3}', "2026-10-01T00:00:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
