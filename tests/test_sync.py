import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tfs import config, password_store
from tfs.cli import main
from tfs.importer import raw_store
from tfs.importer.session import sync
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

NOW = "2026-10-27T07:00:00Z"
NAME = "fake_fetch_connector"
MODULE = "tests.fake_fetch_connector.connector"
SECRET = "s3cret"


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(
            os.environ, {"TFS_HOME": home.name, "TFS_PASSWORD_STORE": "file"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        password_store.set(f"{NAME}:api_key", SECRET)
        config.data_dir().mkdir(parents=True, exist_ok=True)
        self.conn = connect(config.db_path())
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO sources (connector, cursor_json) VALUES (?, ?)", (NAME, '{"n": 1}')
        )

    def plan(self, **plan) -> None:
        patch = mock.patch.dict(os.environ, {"FAKE_FETCH": json.dumps(plan)})
        patch.start()
        self.addCleanup(patch.stop)

    def sync(self):
        return sync(self.conn, config.data_dir(), NAME, now=NOW, module=MODULE)

    def source(self) -> dict:
        return dict(
            self.conn.execute("SELECT * FROM sources WHERE connector = ?", (NAME,)).fetchone()
        )

    def run_cli(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["sync", NAME, "--module", MODULE])
        return code, out.getvalue(), err.getvalue()

    def files(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def records(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]

    def test_two_files_are_saved_then_the_cursor_moves_and_records_are_stored(self):
        self.plan(files=2, cursor={"n": 2})
        result = self.sync()
        self.assertEqual((result.connector, result.files_saved, result.needs_you), (NAME, 2, None))
        self.assertEqual(len(result.results), 2)
        source = self.source()
        self.assertEqual(source["cursor_json"], '{"n": 2}')
        self.assertEqual(source["last_contact_at"], NOW)
        self.assertEqual(source["last_attempt_at"], NOW)
        self.assertEqual(self.files(), 2)
        self.assertEqual(self.records(), 2)

    def test_a_failed_save_keeps_the_cursor_and_the_files_already_saved(self):
        self.plan(files=2, cursor={"n": 2})
        saves = []

        def save_second_fails(*args, **kwargs):
            saves.append(args)
            if len(saves) == 2:
                raise OSError("disk full")
            return raw_store.save_raw(*args, **kwargs)

        with mock.patch("tfs.importer.session.save_raw", save_second_fails):
            result = self.sync()
        self.assertEqual(result.files_saved, 1)
        source = self.source()
        self.assertEqual((source["cursor_json"], source["last_error"]), ('{"n": 1}', "disk full"))
        self.assertEqual(self.files(), 1)

    def test_auth_failed_keeps_the_cursor_and_asks_for_a_reconnect(self):
        self.plan(files=0, cursor={"n": 1}, auth_failed=True)
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(err, f"Reconnect Fake fetch: tfs connect {NAME}\n")
        source = self.source()
        self.assertEqual((source["cursor_json"], source["last_error"]), ('{"n": 1}', "auth_failed"))

    def test_a_secret_in_a_fetch_error_reaches_neither_the_row_nor_the_output(self):
        self.plan(files=0, cursor={"n": 9}, **{"raise": True})
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        source = self.source()
        self.assertEqual(source["cursor_json"], '{"n": 1}')
        for text in (source["last_error"], out, err):
            self.assertNotIn(SECRET, text)
        self.assertIn("[secret]", source["last_error"])

    def test_no_files_still_moves_the_cursor_and_counts_as_contact(self):
        self.conn.execute("UPDATE sources SET last_error = 'old' WHERE connector = ?", (NAME,))
        self.plan(files=0, cursor={"n": 3})
        result = self.sync()
        self.assertEqual((result.files_saved, result.needs_you), (0, None))
        source = self.source()
        self.assertEqual(source["cursor_json"], '{"n": 3}')
        self.assertEqual(source["last_contact_at"], NOW)
        self.assertIsNone(source["last_error"])

    def test_a_missing_secret_counts_as_auth_failed(self):
        password_store.delete(f"{NAME}:api_key")
        self.plan(files=2, cursor={"n": 2})
        result = self.sync()
        self.assertEqual(result.needs_you, f"Reconnect Fake fetch: tfs connect {NAME}")
        self.assertEqual(self.source()["last_error"], "auth_failed")
        self.assertEqual(self.files(), 0)

    def test_a_connector_that_does_not_fetch_is_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(ValueError, "^Apple Health export does not fetch.$"):
            sync(self.conn, config.data_dir(), "apple_health", now=NOW)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE connector = 'apple_health'"
            ).fetchone()[0],
            0,
        )

    def test_the_summary_counts_only_this_connectors_records(self):
        other = raw_store.save_raw(
            self.conn,
            config.data_dir(),
            "other_app",
            "page",
            "json",
            io.BytesIO(b"page 7"),
            now=NOW,
        )
        self.conn.execute("UPDATE files SET status = 'failed' WHERE sha256 = ?", (other.sha256,))
        self.plan(files=0, cursor={"n": 3})
        self.assertEqual(self.sync().summary, "Fake fetch: 0 files, 0 records stored.")


if __name__ == "__main__":
    unittest.main()
