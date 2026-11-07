import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_proposals import ADD, NOW, proposal
from tfs import config, password_store
from tfs.cli import main
from tfs.persistence import sqlite
from tfs.proposals.apply import preview, save
from tfs.scheduling import jobs

MODULE = "tests.fake_connect_connector.connector"
SECRET = "s3cret"


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(
            os.environ, {"TFS_HOME": home.name, "TFS_PASSWORD_STORE": "file"}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def connect(self, *argv: str, secret: str = SECRET) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("getpass.getpass", return_value=secret),
            mock.patch("builtins.input", return_value="eu"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(["connect", *argv])
        return code, out.getvalue(), err.getvalue()

    def source_rows(self) -> list[tuple]:
        conn = sqlite.connect(config.db_path(), "read_only")
        try:
            return [tuple(row) for row in conn.execute("SELECT connector, enabled FROM sources")]
        finally:
            conn.close()

    def test_connect_stores_the_secret_and_turns_the_source_on(self):
        code, out, err = self.connect("fake_connector", "--module", MODULE)
        self.assertEqual(
            (code, out, err),
            (0, "Connected Fake connect. Run tfs sync to fetch your data.\n", ""),
        )
        self.assertEqual(password_store.get("fake_connector:api_key"), SECRET)
        self.assertIsNone(password_store.get("fake_connector:region"))
        self.assertEqual(self.source_rows(), [("fake_connector", 1)])

    def test_a_connector_error_is_printed_and_nothing_is_stored(self):
        code, out, err = self.connect("fake_connector", "--module", MODULE, secret="")
        self.assertEqual((code, out, err), (1, "", "Enter your API key.\n"))
        self.assertIsNone(password_store.get("fake_connector:api_key"))
        self.assertFalse(config.db_path().exists())

    def test_the_secret_is_in_no_output_and_no_database_row(self):
        _, out, err = self.connect("fake_connector", "--module", MODULE)
        conn = sqlite.connect(config.db_path(), "read_only")
        try:
            dump = "\n".join(conn.iterdump())
        finally:
            conn.close()
        for text in (out, err, dump):
            self.assertNotIn(SECRET, text)

    def test_reconnect_resumes_only_auth_blocked_jobs_through_reconciliation(self):
        self.connect("fake_connector", "--module", MODULE)
        manifest = {"label": "Invented app", "sends": ["strength"]}
        with mock.patch.object(jobs, "load_manifests", return_value={"fake_connector": manifest}):
            value = proposal(ADD, {**ADD, "item": "p2"}, {**ADD, "item": "p3"})
            value["delivery_targets"] = preview(config.db_path(), value, now=NOW)[
                "delivery_targets"
            ]
            save(config.db_path(), value, request_id="save-1", approved_by="chat", now=NOW)
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            conn.execute("UPDATE jobs SET status='needs_you', result_json='{}'")
            conn.execute("UPDATE jobs SET result_json='{\"auth_failed\":true}' WHERE item_id='p1'")
            conn.execute("UPDATE jobs SET status='unknown' WHERE item_id='p3'")
            ids = [row[0] for row in conn.execute("SELECT id FROM jobs ORDER BY item_id")]
            code, _, err = self.connect("fake_connector", "--module", MODULE)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(
                [row[0] for row in conn.execute("SELECT status FROM jobs ORDER BY item_id")],
                ["in_doubt", "needs_you", "unknown"],
            )
            self.assertEqual(
                [row[0] for row in conn.execute("SELECT id FROM jobs ORDER BY item_id")], ids
            )

    def test_an_unknown_connector_is_refused(self):
        code, out, err = self.connect("unknown")
        self.assertEqual((code, out, err), (2, "", "No connector named unknown.\n"))
        self.assertFalse(Path(config.data_dir(), "secrets.json").exists())


if __name__ == "__main__":
    unittest.main()
