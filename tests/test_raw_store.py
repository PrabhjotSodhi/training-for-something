import io
import tempfile
import unittest
from pathlib import Path

from tfs.importer.raw_store import FileConflictError, save_raw
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
FIRST = "2026-10-27T07:00:00Z"
SECOND = "2026-10-27T08:00:00Z"


class FailingSource:
    def read(self, size: int = -1) -> bytes:
        raise OSError("disk went away")


class SaveRawTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        self.conn = connect(self.data_dir / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def save(self, data: bytes, now: str, connector="hevy", fmt="workouts_page", ext="json", **kw):
        return save_raw(
            self.conn, self.data_dir, connector, fmt, ext, io.BytesIO(data), now=now, **kw
        )

    def source_row(self, connector: str = "hevy"):
        return self.conn.execute(
            "SELECT * FROM sources WHERE connector = ?", (connector,)
        ).fetchone()

    def files_on_disk(self) -> list[str]:
        raw = self.data_dir / "raw"
        return sorted(str(p.relative_to(raw)) for p in raw.rglob("*") if p.is_file())

    def test_first_save_stores_the_file_and_registers_it(self):
        saved = self.save(b"hello", FIRST)

        expected = self.data_dir / "raw" / "hevy" / "workouts_page" / f"{HELLO_SHA256}.json"
        self.assertEqual(saved.path, expected)
        self.assertEqual(expected.read_bytes(), b"hello")
        self.assertEqual((saved.sha256, saved.arrival_seq, saved.is_new), (HELLO_SHA256, 1, True))
        row = self.conn.execute("SELECT * FROM files").fetchone()
        self.assertEqual((row["status"], row["size_bytes"]), ("new", 5))
        self.assertEqual(row["received_at"], FIRST)
        source = self.source_row()
        self.assertEqual((source["last_contact_at"], source["last_received_at"]), (FIRST, FIRST))

    def test_sending_the_same_bytes_again_adds_nothing(self):
        self.save(b"hello", FIRST)

        saved = self.save(b"hello", SECOND)

        self.assertEqual((saved.arrival_seq, saved.is_new), (1, False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
        self.assertEqual(self.source_row()["last_contact_at"], SECOND)

    def test_a_new_file_gets_the_next_number_and_stores_the_cursor(self):
        self.save(b"hello", FIRST)
        cursor = '{"since": "2026-10-01T00:00:00Z"}'

        saved = self.save(b"world", SECOND, cursor_json=cursor)

        self.assertEqual(saved.arrival_seq, 2)
        self.assertEqual(self.source_row()["cursor_json"], cursor)

    def test_the_same_bytes_from_another_connector_are_refused(self):
        self.save(b"hello", FIRST)

        with self.assertRaises(FileConflictError) as caught:
            self.save(b"hello", SECOND, connector="strava")

        self.assertEqual(
            str(caught.exception), "This file was already received as hevy/workouts_page."
        )
        self.assertEqual(self.files_on_disk(), [f"hevy/workouts_page/{HELLO_SHA256}.json"])

    def test_a_format_that_climbs_out_of_the_folder_is_refused(self):
        with self.assertRaises(ValueError):
            self.save(b"hello", FIRST, fmt="../x")

        self.assertEqual(self.files_on_disk(), [])

    def test_a_failing_source_leaves_no_file_and_no_row(self):
        with self.assertRaises(OSError):
            save_raw(
                self.conn,
                self.data_dir,
                "hevy",
                "workouts_page",
                "json",
                FailingSource(),
                now=FIRST,
            )

        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_the_same_bytes_with_another_extension_keep_one_file(self):
        first = self.save(b"hello", FIRST)

        again = self.save(b"hello", SECOND, ext="txt")

        self.assertEqual(again.path, first.path)
        self.assertEqual(self.files_on_disk(), [f"hevy/workouts_page/{HELLO_SHA256}.json"])


if __name__ == "__main__":
    unittest.main()
