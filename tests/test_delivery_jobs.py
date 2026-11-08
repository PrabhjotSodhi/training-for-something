import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_proposals import ADD, NOW, proposal
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import preview, save, undo


class DeliveryJobsTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tfs.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        manifests = {
            "invented_a": {"label": "Invented A", "sends": ["strength"]},
            "invented_b": {"label": "Invented B", "sends": ["strength"]},
        }
        patch = mock.patch("tfs.scheduling.jobs.load_manifests", return_value=manifests)
        patch.start()
        self.addCleanup(patch.stop)
        self.conn.execute("INSERT INTO sources(connector, enabled) VALUES ('invented_a',1)")

    def approve(self, value: dict) -> dict:
        value = copy.deepcopy(value)
        value["delivery_targets"] = preview(self.path, value, now=NOW)["delivery_targets"]
        return value

    def save(self, value: dict, request: str) -> dict:
        return save(self.path, value, request_id=request, approved_by="chat", now=NOW)

    def undo(self, version: int) -> dict:
        return undo(self.path, version, request_id=f"undo-{version}", approved_by="chat", now=NOW)

    def rows(self) -> list:
        return [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY plan_version")]

    def snapshot(self) -> list:
        return [
            [tuple(row) for row in self.conn.execute(query)]
            for query in (
                "SELECT * FROM journal",
                "SELECT * FROM plan_items",
                "SELECT * FROM session_parts",
                "SELECT * FROM jobs",
            )
        ]

    def test_preview_exact_payload_and_retry_without_duplicates(self) -> None:
        value = proposal(ADD)
        result = preview(self.path, value, now=NOW)
        self.assertEqual(result["delivery_targets"], {"p1": ["invented_a"]})
        self.assertEqual(result["delivery_apps"], {"invented_a": "Invented A"})
        self.assertEqual(self.snapshot(), [[], [], [], []])
        with self.assertRaisesRegex(ValueError, "targets"):
            self.save(value, "missing-targets")
        value["delivery_targets"] = result["delivery_targets"]
        receipt = self.save(value, "save-1")
        row = self.rows()[0]
        self.assertEqual(json.loads(row["payload_json"]), result["items"][0])
        self.assertEqual(
            (row["connector"], row["status"], row["attempts"]), ("invented_a", "pending", 0)
        )
        self.assertEqual(row["created_at"], "2026-11-06T23:00:00Z")
        before = self.snapshot()
        self.assertEqual(self.save(value, "save-1"), receipt)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute("SELECT format_version FROM journal").fetchone()[0], 3)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("UPDATE jobs SET payload_json = '{}' WHERE id = ?", (row["id"],))

    def test_changed_connected_targets_require_a_new_preview(self) -> None:
        value = self.approve(proposal(ADD))
        self.conn.execute("INSERT INTO sources(connector, enabled) VALUES ('invented_b',1)")
        with self.assertRaisesRegex(ValueError, "targets"):
            self.save(value, "save-1")
        self.assertEqual(self.snapshot(), [[], [], [], []])
        updated = self.approve(value)
        self.assertEqual(updated["delivery_targets"], {"p1": ["invented_a", "invented_b"]})
        self.conn.execute("UPDATE sources SET enabled=0 WHERE connector='invented_a'")
        with self.assertRaisesRegex(ValueError, "targets"):
            self.save(updated, "save-1")
        self.save(self.approve(updated), "save-1")
        self.assertEqual([row["connector"] for row in self.rows()], ["invented_b"])

    def test_job_failure_rolls_back_plan_receipt_and_cancellation(self) -> None:
        self.save(self.approve(proposal(ADD)), "save-1")
        before = self.snapshot()
        self.conn.executescript(
            "CREATE TRIGGER refuse_job BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'invented failure'); END;"
        )
        edit = self.approve(
            proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1)
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invented failure"):
            self.save(edit, "save-2")
        self.assertEqual(self.snapshot(), before)

    def test_replace_undo_and_redo_preserve_original_ids_and_payloads(self) -> None:
        self.save(self.approve(proposal(ADD)), "save-1")
        self.conn.execute(
            "UPDATE jobs SET status='not_sent', next_attempt_at='2026-11-08T00:00:00Z'"
        )
        first = self.rows()[0]
        edit = self.approve(
            proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1)
        )
        self.save(edit, "save-2")
        second = self.rows()[1]
        self.assertEqual([r["status"] for r in self.rows()], ["cancelled", "pending"])
        self.undo(2)
        rows = self.rows()
        self.assertEqual(rows[0], first)
        self.assertEqual(rows[1], {**second, "status": "cancelled"})
        self.assertEqual(self.conn.execute("SELECT label FROM plan_items").fetchone()[0], "Push")
        self.undo(3)
        self.assertEqual(self.rows(), [{**first, "status": "cancelled"}, second])
        self.assertEqual(self.conn.execute("SELECT label FROM plan_items").fetchone()[0], "Lighter")

    def test_done_and_remove_cancel_pending_work_and_undo_restores_it(self) -> None:
        self.save(self.approve(proposal(ADD)), "save-1")
        first = self.rows()[0]
        for version, action in ((1, "mark_done"), (3, "remove_session")):
            value = self.approve(proposal({"action": action, "item": "p1"}, version=version))
            self.assertEqual(value["delivery_targets"], {})
            self.save(value, f"save-{version+1}")
            self.assertEqual(self.rows(), [{**first, "status": "cancelled"}])
            self.undo(version + 1)
            self.assertEqual(self.rows(), [first])

    def test_add_then_remove_and_local_only_changes_enqueue_nothing(self) -> None:
        value = proposal(ADD, {"action": "remove_session", "item": "p1"})
        self.save(self.approve(value), "save-1")
        self.assertEqual(self.rows(), [])
        self.undo(1)
        self.assertEqual(self.rows(), [])
        self.conn.execute("UPDATE sources SET enabled=0")
        self.save(proposal({**ADD, "item": "p2"}, version=2), "save-3")
        self.assertEqual(self.rows(), [])

    def test_delivery_states_refuse_save_and_undo_without_partial_changes(self) -> None:
        self.save(self.approve(proposal(ADD)), "save-1")
        for status in ("sending", "in_doubt", "sent", "unknown", "conflict", "needs_you"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE jobs SET status=?", (status,))
                before = self.snapshot()
                value = proposal({"action": "remove_session", "item": "p1"}, version=1)
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    self.approve(value)
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    self.save(value, "save-2")
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    self.undo(1)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_target_shapes_are_plain_errors(self) -> None:
        for targets in ([], {"p1": None}, {"p1": [True]}, {"p1": ["b", "a"]}, {"p1": ["a", "a"]}):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                preview(self.path, {**proposal(ADD), "delivery_targets": targets}, now=NOW)
