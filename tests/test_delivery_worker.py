import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests.helpers import open_database
from tests.test_proposals import ADD, NOW, proposal
from tfs.connectors.core import runner, send
from tfs.connectors.core.contract import to_utc_text
from tfs.mcp.tool_access import describe, open_reader, query
from tfs.persistence.sqlite import connect, transaction
from tfs.proposals.apply import preview, preview_undo, save, undo
from tfs.scheduling import jobs

STAMP = to_utc_text(NOW)
MANIFEST = {
    "label": "Invented app",
    "sends": ["strength"],
    "asks": [{"key": "key", "secret": True}],
}
SENT = {"status": "sent", "remote_id": "routine-1", "remote_version": "version-1"}


class DeliveryWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        self.path = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
        self.conn.execute("INSERT INTO sources(connector) VALUES ('invented')")
        for patch in (
            mock.patch.object(jobs, "load_manifests", return_value={"invented": MANIFEST}),
            mock.patch.object(send.password_store, "get", return_value="invented-secret"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.save(proposal(ADD), "save-1")

    def save(self, value: dict, request: str) -> None:
        value["delivery_targets"] = preview(self.path, value, now=NOW)["delivery_targets"]
        save(self.path, value, request_id=request, approved_by="chat", now=NOW)

    def row(self) -> dict:
        return dict(
            self.conn.execute("SELECT * FROM jobs WHERE status != ?", ("cancelled",)).fetchone()
        )

    def test_success_claims_before_network_and_records_link_atomically(self) -> None:
        original = self.row()

        def deliver(connector, op, payload, **options):
            self.assertEqual((connector, op), ("invented", "send"))
            self.assertEqual(payload["item"], json.loads(original["payload_json"]))
            self.assertEqual(payload["op_id"], original["id"])
            self.assertFalse(payload["in_doubt"])
            self.assertIsNone(payload["remote_id"])
            with closing(connect(self.path, "save")) as other, transaction(other):
                self.assertEqual(other.execute("SELECT status FROM jobs").fetchone()[0], "sending")
            self.assertFalse(jobs.run_one(self.path, STAMP))
            return SENT

        with mock.patch.object(send.runner, "run", side_effect=deliver):
            self.assertTrue(jobs.run_one(self.path, STAMP))
        self.assertEqual((self.row()["status"], self.row()["attempts"]), ("sent", 1))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT remote_id, remote_version, last_job_id FROM remote_links"
                ).fetchone()
            ),
            ("routine-1", "version-1", original["id"]),
        )
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "sent")
        with closing(open_reader(self.path)) as reader:
            result = query(
                reader,
                "SELECT op_id, status, attempts, remote_id, remote_version FROM coach_v1_deliveries",
            )
            self.assertEqual(
                result["rows"], [[original["id"], "sent", 1, "routine-1", "version-1"]]
            )
            self.assertNotIn("payload_json", describe(reader)["coach_v1_deliveries"]["columns"])

    def test_edit_delivered_plan_updates_same_remote_and_keeps_receipts(self) -> None:
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
        first = self.row()
        self.save(
            proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1),
            "edit",
        )
        second = dict(self.conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "saved")
        self.assertNotEqual(first["id"], second["id"])
        self.save(
            proposal({"action": "move_session", "item": "p1", "to": "2026-11-10"}, version=2),
            "move",
        )
        third = dict(self.conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone())
        self.assertEqual(
            self.conn.execute("SELECT status FROM jobs WHERE id=?", (second["id"],)).fetchone()[0],
            "cancelled",
        )
        with mock.patch.object(
            send.runner, "run", return_value={**SENT, "remote_version": "version-2"}
        ) as call:
            self.assertTrue(jobs.run_one(self.path, STAMP))
            payload = call.call_args.args[2]
            self.assertEqual(
                (payload["op_id"], payload["remote_id"], payload["remote_version"]),
                (third["id"], "routine-1", "version-1"),
            )
            self.assertEqual(
                (payload["item"]["label"], payload["item"]["local_date"]), ("Lighter", "2026-11-10")
            )
        self.assertEqual(
            dict(self.conn.execute("SELECT * FROM jobs WHERE id=?", (first["id"],)).fetchone()),
            first,
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT remote_id, remote_version, last_job_id FROM remote_links"
                ).fetchone()
            ),
            ("routine-1", "version-2", third["id"]),
        )
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "sent")

    def test_delivered_edit_undo_preview_binding_and_redo_keep_receipts(self):
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
            self.save(
                proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1),
                "edit",
            )
            jobs.run_one(self.path, STAMP)
        receipts = [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")]
        history = [tuple(row) for row in self.conn.execute("SELECT * FROM journal")]
        self.conn.execute("INSERT INTO sources(connector) VALUES ('unlinked')")
        with mock.patch.object(
            jobs, "load_manifests", return_value={"invented": MANIFEST, "unlinked": MANIFEST}
        ):
            shown = preview_undo(self.path, 2, now=NOW)
        self.assertEqual(shown["delivery_targets"], {"p1": ["invented"]})
        self.assertEqual(shown["items"][0]["label"], ADD["label"])
        self.assertEqual(shown["items"][0]["parts"][0]["weight_kg"], ADD["parts"][0]["weight_kg"])
        self.assertEqual(
            [tuple(row) for row in self.conn.execute("SELECT * FROM journal")], history
        )
        with self.assertRaisesRegex(ValueError, "approval"):
            undo(self.path, 2, request_id="missing-approval", approved_by="chat", now=NOW)
        self.conn.execute("UPDATE sources SET enabled=0 WHERE connector='invented'")
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            undo(
                self.path,
                2,
                request_id="disconnected",
                approved_by="chat",
                now=NOW,
                delivery_targets=shown["delivery_targets"],
            )
        self.conn.execute("UPDATE sources SET enabled=1 WHERE connector='invented'")
        for _ in range(2):
            result = undo(
                self.path,
                2,
                request_id="compensate",
                approved_by="chat",
                now=NOW,
                delivery_targets=shown["delivery_targets"],
            )
            self.assertEqual(result["plan_version"], 3)
        queued = dict(self.conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone())
        self.assertNotIn(queued["id"], {row["id"] for row in receipts})
        self.assertEqual(json.loads(queued["payload_json"])["label"], ADD["label"])
        with mock.patch.object(
            send.runner, "run", return_value={**SENT, "remote_version": "version-2"}
        ):
            jobs.run_one(self.path, STAMP)
        redo = preview_undo(self.path, 3, now=NOW)
        undo(
            self.path,
            3,
            request_id="redo",
            approved_by="chat",
            now=NOW,
            delivery_targets=redo["delivery_targets"],
        )
        newest = dict(self.conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone())
        self.assertNotEqual(newest["id"], queued["id"])
        self.assertEqual(json.loads(newest["payload_json"])["label"], "Lighter")
        for row in receipts:
            self.assertEqual(
                dict(self.conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()),
                row,
            )
        self.assertEqual(
            self.conn.execute("SELECT format_version FROM journal WHERE version=2").fetchone()[0], 5
        )

    def test_undo_refuses_new_external_copies_without_erasing_history(self):
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
            with self.assertRaisesRegex(ValueError, "reconciliation"):
                preview_undo(self.path, 1, now=NOW)
            self.conn.execute("INSERT INTO sources(connector) VALUES ('new_app')")
            with mock.patch.object(
                jobs, "load_manifests", return_value={"invented": MANIFEST, "new_app": MANIFEST}
            ):
                self.save(
                    proposal(
                        {"action": "change_session", "item": "p1", "label": "Lighter"}, version=1
                    ),
                    "edit",
                )
                jobs.run_one(self.path, STAMP)
                jobs.run_one(self.path, STAMP)
                before = [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")]
                with self.assertRaisesRegex(ValueError, "prior remote copies"):
                    preview_undo(self.path, 2, now=NOW)
                with self.assertRaisesRegex(ValueError, "prior remote copies"):
                    undo(
                        self.path,
                        2,
                        request_id="undo-new-copy",
                        approved_by="chat",
                        now=NOW,
                        delivery_targets={"p1": ["invented", "new_app"]},
                    )
                self.assertEqual(
                    [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")],
                    before,
                )
                self.assertEqual(
                    self.conn.execute("SELECT max(version) FROM journal").fetchone()[0], 2
                )

    def test_delivered_edit_refuses_disconnected_app_and_uncertain_replacement(self) -> None:
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
        value = proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1)
        value["delivery_targets"] = preview(self.path, value, now=NOW)["delivery_targets"]
        self.conn.execute("UPDATE sources SET enabled=0")
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            save(self.path, value, request_id="disabled", approved_by="chat", now=NOW)
        with self.assertRaisesRegex(ValueError, "Reconnect"):
            preview(self.path, value, now=NOW)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 1)
        self.conn.execute("UPDATE sources SET enabled=1")
        self.save(value, "edit")
        with mock.patch.object(send.runner, "run", return_value={"status": "unknown"}):
            jobs.run_one(self.path, STAMP)
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.save(
                proposal({"action": "change_session", "item": "p1", "label": "Again"}, version=2),
                "uncertain",
            )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT label FROM plan_items").fetchone()[0], "Lighter")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            preview_undo(self.path, 2, now=NOW)

    def test_undo_partial_delivery_preserves_acknowledged_receipt(self) -> None:
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
            self.conn.execute("INSERT INTO sources(connector) VALUES ('invented_b')")
            with mock.patch.object(
                jobs, "load_manifests", return_value={"invented": MANIFEST, "invented_b": MANIFEST}
            ):
                for version in (1, 2, 3):
                    self.save(
                        proposal(
                            {"action": "change_session", "item": "p1", "label": f"Edit {version}"},
                            version=version,
                        ),
                        f"edit-{version}",
                    )
                    if version == 1:
                        self.assertTrue(jobs.run_one(self.path, STAMP))
                        self.assertTrue(jobs.run_one(self.path, STAMP))
                self.assertTrue(jobs.run_one(self.path, STAMP))
                self.assertEqual(
                    self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "saved"
                )
                queries = (
                    "SELECT * FROM jobs ORDER BY id",
                    "SELECT * FROM remote_links ORDER BY connector",
                    "SELECT * FROM journal",
                    "SELECT * FROM plan_items",
                )
                before = [[tuple(r) for r in self.conn.execute(q)] for q in queries]
                with self.assertRaisesRegex(ValueError, "approval"):
                    undo(self.path, 4, request_id="undo-partial", approved_by="chat", now=NOW)
                self.assertEqual(
                    [[tuple(r) for r in self.conn.execute(q)] for q in queries], before
                )
                acknowledged = [
                    dict(row) for row in self.conn.execute("SELECT * FROM jobs WHERE status='sent'")
                ]
                shown = preview_undo(self.path, 4, now=NOW)
                undo(
                    self.path,
                    4,
                    request_id="approved-partial",
                    approved_by="chat",
                    now=NOW,
                    delivery_targets=shown["delivery_targets"],
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM jobs WHERE status='pending'"
                    ).fetchone()[0],
                    2,
                )
                for row in acknowledged:
                    self.assertEqual(
                        dict(
                            self.conn.execute(
                                "SELECT * FROM jobs WHERE id=?", (row["id"],)
                            ).fetchone()
                        ),
                        row,
                    )

    def test_manual_done_after_delivery_cancels_only_pending_replacement(self) -> None:
        with mock.patch.object(send.runner, "run", return_value=SENT):
            jobs.run_one(self.path, STAMP)
        first = self.row()
        self.save(
            proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1),
            "edit",
        )
        self.save(proposal({"action": "mark_done", "item": "p1"}, version=2), "done")
        self.assertEqual(
            dict(self.conn.execute("SELECT * FROM jobs WHERE id=?", (first["id"],)).fetchone()),
            first,
        )
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "done")
        self.assertFalse(jobs.run_one(self.path, STAMP))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM jobs WHERE status='cancelled'").fetchone()[0], 1
        )

    def test_definite_failure_waits_five_minutes_but_uncertainty_never_retries(self) -> None:
        with mock.patch.object(send.runner, "run", return_value={"status": "not_sent"}) as call:
            self.assertTrue(jobs.run_one(self.path, STAMP))
            self.assertEqual(self.row()["next_attempt_at"], "2026-11-06T23:05:00Z")
            self.assertFalse(jobs.run_one(self.path, STAMP))
            call.side_effect = runner.RunnerTimeoutError("invented-secret")
            self.assertTrue(jobs.run_one(self.path, "2026-11-06T23:05:00Z"))
            self.assertEqual(self.row()["status"], "unknown")
            jobs.recover(self.conn)
            self.assertFalse(jobs.run_one(self.path, "2027-01-01T00:00:00Z"))
            self.assertEqual(call.call_count, 2)
            self.assertNotIn("invented-secret", self.row()["result_json"])

    def test_restart_reconciles_sending_with_same_id_and_existing_remote_reference(self) -> None:
        original = self.row()
        self.conn.execute("UPDATE jobs SET status='sending', attempts=1")
        self.conn.execute(
            "INSERT INTO remote_links VALUES (?, ?, ?, ?, ?)",
            ("p1", "invented", "known-id", "known-version", original["id"]),
        )
        jobs.recover(self.conn)
        with mock.patch.object(send.runner, "run", return_value=SENT) as call:
            jobs.run_one(self.path, STAMP)
            payload = call.call_args.args[2]
            self.assertTrue(payload["in_doubt"])
            self.assertEqual(
                (payload["op_id"], payload["remote_id"], payload["remote_version"]),
                (original["id"], "known-id", "known-version"),
            )
            self.assertEqual(self.row()["attempts"], 2)

    def test_failure_recording_link_leaves_job_for_reconciliation(self) -> None:
        self.conn.executescript(
            "CREATE TRIGGER refuse_link BEFORE INSERT ON remote_links BEGIN SELECT RAISE(ABORT, 'invented failure'); END;"
        )

        with (
            mock.patch.object(send.runner, "run", return_value=SENT),
            self.assertRaises(sqlite3.IntegrityError),
        ):
            jobs.run_one(self.path, STAMP)
        self.assertEqual(self.row()["status"], "sending")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM remote_links").fetchone()[0], 0)
        jobs.recover(self.conn)
        self.assertEqual(self.row()["status"], "in_doubt")

    def test_restored_job_can_send_after_undo_advances_the_plan_version(self) -> None:
        original = self.row()
        self.save(
            proposal({"action": "change_session", "item": "p1", "label": "Lighter"}, version=1),
            "save-2",
        )
        undo(self.path, 2, request_id="undo-2", approved_by="chat", now=NOW)
        with mock.patch.object(send.runner, "run", return_value=SENT) as call:
            jobs.run_one(self.path, STAMP)
            self.assertEqual(call.call_args.args[2]["op_id"], original["id"])
            self.assertEqual(call.call_args.args[2]["item"], json.loads(original["payload_json"]))
        self.assertEqual(
            tuple(self.conn.execute("SELECT plan_version, status FROM plan_items").fetchone()),
            (3, "sent"),
        )

    def test_disabled_source_or_capability_does_not_send(self) -> None:
        with mock.patch.object(send.runner, "run") as call:
            self.conn.execute("UPDATE sources SET enabled=0")
            self.assertFalse(jobs.run_one(self.path, STAMP))
            self.conn.execute("UPDATE sources SET enabled=1")
            with mock.patch.object(
                jobs, "load_manifests", return_value={"invented": {**MANIFEST, "sends": []}}
            ):
                self.assertFalse(jobs.run_one(self.path, STAMP))
            call.assert_not_called()
        self.assertEqual((self.row()["status"], self.row()["attempts"]), ("pending", 0))

    def test_missing_credentials_and_safe_outcome_shapes(self) -> None:
        job = {
            **self.row(),
            "item": {},
            "remote_id": None,
            "remote_version": None,
            "in_doubt": False,
        }
        with (
            mock.patch.object(send.password_store, "get", return_value=None),
            mock.patch.object(send.runner, "run") as call,
        ):
            self.assertEqual(send.deliver(job, MANIFEST, STAMP)["status"], "needs_you")
            call.assert_not_called()
        for result, expected in (
            ({"unsupported": True}, "needs_you"),
            ({"status": "not_sent", "auth_failed": True}, "needs_you"),
            ({"status": "sent", "remote_id": "x"}, "unknown"),
            ({**SENT, "remote_id": "invented-secret"}, "unknown"),
            (
                {
                    "status": "conflict",
                    "message": "Contains invented-secret",
                    "extra": "invented-secret",
                },
                "conflict",
            ),
            ({"status": "needs_you", "message": "Delete it in the app"}, "needs_you"),
            ({"status": "made_up"}, "unknown"),
        ):
            with (
                self.subTest(result=result),
                mock.patch.object(send.runner, "run", return_value=result),
            ):
                safe = send.deliver(job, MANIFEST, STAMP)
                self.assertEqual(safe["status"], expected)
                self.assertNotIn("invented-secret", json.dumps(safe))
