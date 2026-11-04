import contextlib
import http.client
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import crash_child, test_crash_raw
from tests.test_engine import free_port
from tfs import config, password_store
from tfs.connectors.core import runner
from tfs.engine import Engine
from tfs.importer.raw_store import scan_raw
from tfs.importer.session import process_pending, sync
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

TOKEN = "crash-test-token-not-a-real-secret"
PLAN = {"files": 2, "cursor": {"n": 2}}
FILES = "SELECT count(*) FROM files"
RECORDS = "SELECT count(*) FROM source_records"
CURSOR = "SELECT cursor_json FROM sources"
# Client side only. A broken server fails a test instead of hanging it.
SAFETY_TIMEOUT_S = 10


class CrashSyncPushTest(unittest.TestCase):
    new_folder = test_crash_raw.CrashRawTest.new_folder
    snapshot = test_crash_raw.CrashRawTest.snapshot
    count = test_crash_raw.CrashRawTest.count

    def setUp(self) -> None:
        home = self.new_folder()
        environment = mock.patch.dict(
            os.environ,
            {"TFS_HOME": str(home), "TFS_PASSWORD_STORE": "file", "FAKE_FETCH": json.dumps(PLAN)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        password_store.set("push_token", TOKEN)
        password_store.set(f"{crash_child.FETCH_CONNECTOR}:api_key", "invented-key")

    def crash_at(self, point: str, scenario: str) -> Path:
        """Run the scenario in a child process that stops at the point, and return its folder."""
        folder = self.new_folder()
        child = subprocess.run(
            [sys.executable, "-m", "tests.crash_child", point, str(folder), scenario],
            cwd=test_crash_raw.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((child.returncode, child.stderr), (1, ""))
        return folder

    def phone_pushes(self, folder: Path) -> None:
        """Start an engine on the folder, push the body once, and wait for the records."""
        port = free_port()
        running = Engine(folder, screen_port=port, now_fn=lambda: crash_child.PUSH_NOW)
        self.addCleanup(running.stop)
        running.start()
        client = http.client.HTTPConnection(config.SCREEN_HOST, port, timeout=SAFETY_TIMEOUT_S)
        self.addCleanup(client.close)
        client.request(
            "POST",
            f"/push/{crash_child.PUSH_CONNECTOR}",
            body=crash_child.PUSH_BODY.read_bytes(),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(client.getresponse().status, 200)
        running.drain()

    def uncrashed_push(self) -> dict:
        folder = self.new_folder()
        self.phone_pushes(folder)
        found = self.snapshot(folder)
        self.assertGreater(len(found["source_records"]), 0)
        return found

    def restart_and_sync(self, folder: Path) -> dict:
        """Start up as the engine does, then sync again. Return the cursor the fetch was given."""
        with contextlib.closing(connect(folder / "tfs.db")) as conn:
            migrate(conn)
            scan_raw(conn, folder)
            process_pending(conn, folder, module=crash_child.FETCH_MODULE)
            with mock.patch.object(runner, "run", wraps=runner.run) as fetch:
                sync(
                    conn,
                    folder,
                    crash_child.FETCH_CONNECTOR,
                    now=crash_child.NOW,
                    module=crash_child.FETCH_MODULE,
                )
        return fetch.call_args_list[0].args[2]["cursor"]

    def uncrashed_sync(self) -> dict:
        folder = self.new_folder()
        crash_child.operate_sync(folder)
        found = self.snapshot(folder)
        self.assertEqual(len(found["source_records"]), 2)
        self.assertEqual(found["sources"][0]["cursor_json"], '{"n": 2}')
        return found

    def test_a_crash_before_the_answer_leaves_one_file_and_the_resend_changes_nothing(
        self,
    ) -> None:
        expected = self.uncrashed_push()
        folder = self.crash_at("after_push_saved", "push")
        self.assertEqual(self.count(folder, FILES), 1)
        self.assertEqual(self.count(folder, RECORDS), 0)

        self.phone_pushes(folder)

        self.assertEqual(self.count(folder, FILES), 1)
        self.assertEqual(self.snapshot(folder), expected)

    def test_a_crash_before_the_save_commits_loses_nothing_when_the_phone_resends(self) -> None:
        expected = self.uncrashed_push()
        folder = self.crash_at("after_rename", "push")
        self.assertEqual(self.count(folder, FILES), 0)
        self.assertEqual(len(list((folder / "raw").glob("*/*/*.json"))), 1)

        self.phone_pushes(folder)

        found = self.snapshot(folder)
        # Registering the file left on disk sets revision_at from the file time.
        for snapshot in (found, expected):
            for row in snapshot["source_records"]:
                del row["revision_at"]
        self.assertEqual(self.count(folder, FILES), 1)
        self.assertEqual(found, expected)

    def test_a_crash_before_the_cursor_moves_is_fixed_by_the_next_sync(self) -> None:
        expected = self.uncrashed_sync()
        folder = self.crash_at("after_first_page", "sync")
        self.assertEqual(self.count(folder, FILES), 1)
        self.assertIsNone(self.count(folder, CURSOR))

        self.restart_and_sync(folder)

        self.assertEqual(self.snapshot(folder), expected)

    def test_a_crash_before_the_records_are_stored_loses_nothing_and_fetches_only_new_data(
        self,
    ) -> None:
        expected = self.uncrashed_sync()
        folder = self.crash_at("before_sync_read", "sync")
        self.assertEqual(self.count(folder, FILES), 2)
        self.assertEqual(self.count(folder, RECORDS), 0)
        self.assertEqual(self.count(folder, CURSOR), '{"n": 2}')

        with mock.patch.dict(os.environ, {"FAKE_FETCH": json.dumps({**PLAN, "files": 0})}):
            asked_after = self.restart_and_sync(folder)

        self.assertEqual(asked_after, {"n": 2})
        self.assertEqual(self.snapshot(folder), expected)
