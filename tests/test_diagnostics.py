import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from tfs import config
from tfs.cli import main
from tfs.diagnostics import render_text, report
from tfs.persistence import sqlite
from tfs.persistence.migrations import migrate

NOW = "2026-10-27T07:00:00Z"
MANIFESTS = {
    "hevy": {"label": "Hevy", "stale_after_hours": 168},
    "health_auto_export": {"label": "Health Auto Export", "stale_after_hours": 48},
}


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(os.environ, {"TFS_HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)
        config.data_dir().mkdir(parents=True, exist_ok=True)
        self.conn = sqlite.connect(config.db_path())
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def add_source(self, connector, last_contact_at=None, last_error=None):
        self.conn.execute(
            "INSERT INTO sources (connector, last_contact_at, last_error) VALUES (?, ?, ?)",
            (connector, last_contact_at, last_error),
        )

    def add_file(self, seq, status, message=None):
        self.conn.execute(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
            " received_at, status, message) VALUES (?, ?, 'hevy', 'json', 'json', 1, ?, ?, ?)",
            (f"{seq:064x}", seq, NOW, status, message),
        )

    def add_record(self, key, end_utc, deleted_at=None):
        self.add_file(100 + len(key), "ok")
        self.conn.execute(
            "INSERT INTO source_records (id, connector, stream, key, kind, revision_at,"
            " arrival_seq, file_sha256, reader_version, start_utc, end_utc, local_date,"
            " values_json, deleted_at) VALUES (?, 'hevy', 'workouts', ?, 'session', ?, 1, ?,"
            " '1', '2026-10-26T18:00:00Z', ?, '2026-10-26', '{}', ?)",
            (key, key, NOW, f"{100 + len(key):064x}", end_utc, deleted_at),
        )

    def source(self, connector):
        return next(
            s
            for s in report(self.conn, now=NOW, manifests=MANIFESTS)["sources"]
            if s["connector"] == connector
        )

    def test_a_source_in_contact_is_fresh_and_covers_its_latest_record(self):
        self.add_source("hevy", "2026-10-27T06:00:00Z")
        self.add_record("a", "2026-10-26T19:05:00Z")
        self.add_record("bb", "2026-10-26T12:00:00Z")
        self.add_record("ccc", "2026-10-26T23:00:00Z", deleted_at=NOW)
        entry = self.source("hevy")
        self.assertEqual(
            (entry["label"], entry["stale"], entry["covered_until"]),
            ("Hevy", False, "2026-10-26T19:05:00Z"),
        )

    def test_a_source_past_its_limit_or_never_in_contact_is_stale(self):
        self.add_source("health_auto_export", "2026-10-24T07:00:00Z")
        self.add_source("hevy")
        self.assertTrue(self.source("health_auto_export")["stale"])
        self.assertTrue(self.source("hevy")["stale"])

    def test_files_are_counted_and_problems_list_newest_first(self):
        for seq, status in enumerate(["ok", "ok", "partial", "unreadable", "failed", "missing"], 1):
            self.add_file(seq, status, f"message {seq}")
        result = report(self.conn, now=NOW, manifests=MANIFESTS)
        self.assertEqual(
            result["files"],
            {
                "new": 0,
                "reading": 0,
                "ok": 2,
                "partial": 1,
                "unreadable": 1,
                "needs_ai": 0,
                "failed": 1,
                "missing": 1,
            },
        )
        self.assertEqual(
            [(p["sha256"], p["connector"], p["status"], p["message"]) for p in result["problems"]],
            [
                (f"{6:064x}", "hevy", "missing", "message 6"),
                (f"{5:064x}", "hevy", "failed", "message 5"),
                (f"{4:064x}", "hevy", "unreadable", "message 4"),
                (f"{3:064x}", "hevy", "partial", "message 3"),
            ],
        )

    def test_text_names_the_stale_source_and_the_file_problems(self):
        self.add_source("health_auto_export", "2026-10-24T07:00:00Z")
        self.add_file(1, "failed", "Could not read the file.")
        text = render_text(report(self.conn, now=NOW, manifests=MANIFESTS))
        self.assertIn(
            "Health Auto Export   Stale: no contact since 2026-10-24 07:00 UTC", text.splitlines()
        )
        self.assertIn("Could not read the file.", text)

    def test_status_command_prints_the_text_and_exits_0(self):
        self.add_source("hevy", "2026-10-27T06:00:00Z")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Hevy", out.getvalue())

    def test_status_command_without_a_database_says_so_and_exits_1(self):
        self.conn.close()
        config.db_path().unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["status"])
        self.assertEqual(
            (code, err.getvalue()), (1, "TFS has no data yet. Run tfs connect NAME first.\n")
        )
