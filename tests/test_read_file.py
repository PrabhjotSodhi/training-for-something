import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tfs import config
from tfs.connectors.core.runner import reader_version
from tfs.importer import records
from tfs.importer.raw_store import save_raw
from tfs.importer.session import ImportResult, process_pending, read_file, rebuild_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

NOW = "2026-10-27T09:00:00Z"
FAKE = "tests.fake_connector.connector"
FAKE_FOLDER = Path(__file__).parent / "fake_connector"
THIS = "tests.test_read_file"
REJECTED_REASON = "kind must be one of session, measurement, event, document, d"


def read(path: str, fmt: str, out_path: str) -> dict:
    """A reader whose behaviour follows the file's bytes. The runner imports it by name."""
    content = Path(path).read_bytes()
    if content == b"boom":
        raise RuntimeError("the reader broke")
    good = {
        "kind": "event",
        "stream": "events",
        "key": content.decode(),
        "revision_at": None,
        "start_utc": "2026-02-28T18:12:33Z",
        "end_utc": None,
        "utc_offset_min": 0,
        "local_date": "2026-02-28",
        "label": "Invented event",
        "family": None,
        "metric": None,
        "values": {},
        "origin_app": None,
        "origin_device": None,
        "plan_ref": None,
    }
    lines = [json.dumps(good)]
    if content == b"reject":
        lines.append(json.dumps({"kind": "bogus", "stream": "events", "key": "bad"}))
    if content == b"garbage":
        lines = ["this is not json"]
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "ok", "count": len(lines), "message": None}


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.dict(os.environ, {"TFS_HOME": folder.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.data_dir = config.data_dir()
        self.conn = connect(self.data_dir / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def save(self, data: bytes) -> str:
        return save_raw(
            self.conn,
            self.data_dir,
            "fake_connector",
            "records",
            "json",
            io.BytesIO(data),
            now=NOW,
        ).sha256

    def row(self, sha256: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM files WHERE sha256 = ?", (sha256,)).fetchone()

    def dates(self) -> list[str]:
        rows = self.conn.execute("SELECT local_date FROM rebuild_dates ORDER BY 1").fetchall()
        return [row[0] for row in rows]

    def keys(self) -> list[str]:
        rows = self.conn.execute("SELECT key FROM source_records ORDER BY key").fetchall()
        return [row[0] for row in rows]


class ReadFileTest(SessionTest):
    def test_a_registered_file_is_stored_then_its_dates_are_rebuilt(self) -> None:
        sha256 = self.save(b"first")

        result = read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE)

        self.assertEqual(
            result,
            ImportResult(
                sha256, "fake_connector", "ok", None, inserted=1, rejected={}, skipped=False
            ),
        )
        row = self.row(sha256)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["reader_version"], reader_version(folder=FAKE_FOLDER))
        self.assertEqual(self.dates(), ["2026-03-01"])

        self.assertEqual(rebuild_pending(self.conn), 1)
        self.assertEqual(self.dates(), [])

    def test_reading_again_with_the_same_reader_changes_nothing(self) -> None:
        sha256 = self.save(b"first")
        read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE)
        before = tuple(self.row(sha256))

        again = read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE)

        self.assertEqual(
            again, ImportResult(sha256, "fake_connector", "ok", None, skipped=True, rejected={})
        )
        self.assertEqual(tuple(self.row(sha256)), before)

    def test_a_file_that_is_being_read_is_skipped_by_process_pending(self) -> None:
        sha256 = self.save(b"first")
        self.conn.execute("UPDATE files SET status = 'reading' WHERE sha256 = ?", (sha256,))

        self.assertEqual(process_pending(self.conn, self.data_dir, now=NOW, module=FAKE), [])
        self.assertEqual(self.row(sha256)["status"], "reading")
        self.assertTrue(read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE).skipped)

    def test_a_rejected_record_makes_the_file_partial(self) -> None:
        sha256 = self.save(b"reject")

        result = read_file(self.conn, self.data_dir, sha256, now=NOW, module=THIS)

        self.assertEqual(result.rejected, {REJECTED_REASON: 1})
        self.assertEqual(result.inserted, 1)
        row = self.row(sha256)
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["message"], f"1 record rejected: {REJECTED_REASON}")

    def test_an_unexpected_error_marks_the_file_failed_and_it_is_read_again(self) -> None:
        sha256 = self.save(b"first")

        with (
            mock.patch.object(records, "store", side_effect=RuntimeError("invented-secret-77")),
            self.assertLogs("tfs.importer.session", "ERROR") as logs,
        ):
            result = read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.message, "Reader failed: RuntimeError")
        row = self.row(sha256)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["message"], "Reader failed: RuntimeError")
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [f"file processing failed {sha256} RuntimeError"],
        )
        self.assertNotIn("invented-secret-77", "\n".join(logs.output))
        self.assertIsNone(logs.records[0].exc_info)

        again = read_file(self.conn, self.data_dir, sha256, now=NOW, module=FAKE)

        self.assertEqual(again.status, "ok")
        self.assertEqual(self.row(sha256)["status"], "ok")


class ProcessPendingTest(SessionTest):
    def test_one_failing_file_does_not_block_the_next(self) -> None:
        broken = self.save(b"boom")
        fine = self.save(b"fine")

        results = process_pending(self.conn, self.data_dir, now=NOW, module=THIS)

        self.assertEqual([result.sha256 for result in results], [broken, fine])
        self.assertEqual(self.row(broken)["status"], "failed")
        self.assertTrue(self.row(broken)["message"].startswith("Reader failed:"))
        self.assertEqual(self.row(fine)["status"], "ok")
        self.assertEqual(self.keys(), ["fine"])
        self.assertEqual(self.dates(), [])

    def test_failed_files_are_read_again_only_when_asked(self) -> None:
        broken = self.save(b"boom")
        process_pending(self.conn, self.data_dir, now=NOW, module=THIS)

        self.assertEqual(process_pending(self.conn, self.data_dir, now=NOW, module=THIS), [])
        retried = process_pending(self.conn, self.data_dir, now=NOW, retry_failed=True, module=THIS)

        self.assertEqual([result.sha256 for result in retried], [broken])

    def test_an_unexpected_error_is_logged_without_its_message(self) -> None:
        garbage = self.save(b"garbage")
        fine = self.save(b"fine")

        with self.assertLogs("tfs.importer.session", "ERROR") as logs:
            results = process_pending(self.conn, self.data_dir, now=NOW, module=THIS)

        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [f"file processing failed {garbage} JSONDecodeError"],
        )
        self.assertIsNone(logs.records[0].exc_info)
        self.assertEqual([result.status for result in results], ["failed", "ok"])
        row = self.row(garbage)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("this is not json", row["message"])
        self.assertEqual(self.row(fine)["status"], "ok")
