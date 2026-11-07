import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_schema import insert
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import preview, save, undo

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone(timedelta(hours=13)))
ADD = {
    "action": "add_session",
    "item": "p1",
    "date": "2026-11-08",
    "family": "strength",
    "label": "Push",
    "parts": [{"part_kind": "set", "label": "Press", "reps": 8, "weight_kg": 30}],
}


def proposal(*actions: dict, version: int = 0) -> dict:
    return {
        "plan_version": version,
        "why": "Prepare for the goal",
        "sources": [],
        "actions": copy.deepcopy(list(actions)),
    }


class ProposalsTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tfs.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        migrate(self.conn)

    def save(self, value: dict, request: str = "save-1") -> dict:
        return save(self.path, value, request_id=request, approved_by="chat", now=NOW)

    def counts(self) -> tuple:
        return tuple(
            self.conn.execute(
                "SELECT (SELECT count(*) FROM journal), (SELECT count(*) FROM plan_items),"
                " (SELECT count(*) FROM session_parts)"
            ).fetchone()
        )

    def test_preview_and_save_a_batch_in_action_order(self) -> None:
        value = proposal(
            ADD,
            {"action": "move_session", "item": "p1", "to": "2026-11-09"},
            {"action": "change_session", "item": "p1", "label": "Easy push"},
        )
        original = copy.deepcopy(value)
        result = preview(self.path, value, now=NOW)
        self.assertEqual(result["items"][0]["local_date"], "2026-11-09")
        self.assertEqual(result["items"][0]["label"], "Easy push")
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.save(value), {"request_id": "save-1", "plan_version": 1})
        self.assertEqual(value, original)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT local_date, label, version, plan_version, status FROM plan_items"
                ).fetchone()
            ),
            ("2026-11-09", "Easy push", 1, 1, "saved"),
        )
        row = self.conn.execute("SELECT * FROM journal").fetchone()
        self.assertEqual(json.loads(row["before_json"]), {"p1": None})
        self.assertEqual(row["saved_at"], "2026-11-06T23:00:00Z")

    def test_invalid_later_action_and_unknown_fields_save_nothing(self) -> None:
        cases = [
            proposal(ADD, {"action": "move_session", "item": "missing", "to": "2026-11-09"}),
            proposal({**ADD, "send_now": True}),
            proposal({**ADD, "date": "2026-11-06"}),
            proposal({**ADD, "family": "unknown"}),
            proposal({**ADD, "goal_ids": ["missing"]}),
            proposal({**ADD, "parts": [{"part_kind": "set", "reps": True}]}),
            proposal({**ADD, "parts": [{"part_kind": "set", "weight_kg": float("inf")}]}),
            proposal({"action": "set_goal", "item": "g1"}),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save(value)
            self.assertEqual(self.counts(), (0, 0, 0))

    def test_database_failure_rolls_back_journal_plan_and_parts(self) -> None:
        self.conn.executescript(
            "CREATE TRIGGER refuse_parts BEFORE INSERT ON session_parts"
            " BEGIN SELECT RAISE(ABORT, 'invented failure'); END;"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invented failure"):
            self.save(proposal(ADD))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_retry_returns_original_receipt_and_changed_retry_fails(self) -> None:
        value = proposal(ADD)
        receipt = self.save(value)
        self.assertEqual(self.save(value), receipt)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.save(proposal({**ADD, "label": "Different"}))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_stale_proposal_cannot_overwrite_another_saved_change(self) -> None:
        self.save(proposal(ADD))
        next_value = proposal(
            {"action": "change_session", "item": "p1", "label": "Lighter"}, version=1
        )
        preview(self.path, next_value, now=NOW)
        self.save(
            proposal({"action": "move_session", "item": "p1", "to": "2026-11-10"}, version=1),
            "save-2",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.save(next_value, "save-3")
        self.assertEqual(
            tuple(self.conn.execute("SELECT local_date, label FROM plan_items").fetchone()),
            ("2026-11-10", "Push"),
        )

    def test_done_and_removed_items_refuse_further_changes(self) -> None:
        self.save(proposal(ADD, {**ADD, "item": "p2"}))
        self.save(
            proposal(
                {"action": "mark_done", "item": "p1"},
                {"action": "remove_session", "item": "p2"},
                version=1,
            ),
            "save-2",
        )
        for item in ("p1", "p2"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.save(
                    proposal(
                        {"action": "change_session", "item": item, "label": "Wrong"}, version=2
                    ),
                    "save-3",
                )
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute("SELECT id, status FROM plan_items ORDER BY id")
            ],
            [("p1", "done"), ("p2", "removed")],
        )

    def test_move_preserves_part_details_and_imported_rows(self) -> None:
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts", reps=12)
        self.save(proposal(ADD))
        self.conn.execute(
            "UPDATE session_parts SET extras_json = '{\"zone\":2}' WHERE plan_item_id = 'p1'"
        )
        self.save(
            proposal({"action": "move_session", "item": "p1", "to": "2026-11-10"}, version=1),
            "save-2",
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT record_id, plan_item_id, reps, weight_kg, extras_json"
                    " FROM session_parts ORDER BY plan_item_id"
                )
            ],
            [("r1", None, 12, None, None), (None, "p1", 8, 30.0, '{"zone":2}')],
        )
        row = self.conn.execute("SELECT before_json FROM journal WHERE version=2").fetchone()
        self.assertEqual(json.loads(row[0])["p1"]["local_date"], "2026-11-08")

    def test_approval_route_and_aware_time_are_required(self) -> None:
        for route, now in [("automatic", NOW), ("chat", NOW.replace(tzinfo=None))]:
            with self.subTest(route=route), self.assertRaises(ValueError):
                save(self.path, proposal(ADD), request_id="s1", approved_by=route, now=now)
        self.assertEqual(self.counts(), (0, 0, 0))

    def undo(self, version: int, request: str = "undo-1") -> dict:
        return undo(self.path, version, request_id=request, approved_by="chat", now=NOW)

    def test_undo_add_and_retry_preserve_history(self) -> None:
        self.save(proposal(ADD))
        original = tuple(self.conn.execute("SELECT * FROM journal").fetchone())
        self.assertEqual(self.undo(1), {"request_id": "undo-1", "plan_version": 2})
        self.assertEqual(self.undo(1), {"request_id": "undo-1", "plan_version": 2})
        self.assertEqual(
            tuple(
                self.conn.execute("SELECT status, version, plan_version FROM plan_items").fetchone()
            ),
            ("removed", 2, 2),
        )
        self.assertEqual(
            tuple(self.conn.execute("SELECT * FROM journal WHERE version=1").fetchone()), original
        )
        with self.assertRaisesRegex(ValueError, "different content"):
            self.undo(2)
        self.assertEqual(self.counts(), (2, 1, 1))
        self.undo(2, "undo-2")
        self.assertEqual(
            tuple(
                self.conn.execute("SELECT status, version, plan_version FROM plan_items").fetchone()
            ),
            ("saved", 3, 3),
        )

    def test_undo_restores_exact_prior_fields_and_parts(self) -> None:
        insert(self.conn, "files")
        insert(self.conn, "source_records")
        insert(self.conn, "session_parts", reps=12)
        initial = proposal(ADD)
        initial["sources"] = [{"url": "https://example.org/study", "type": "trial"}]
        self.save(initial)
        original = dict(self.conn.execute("SELECT * FROM plan_items").fetchone())
        original_parts = [
            tuple(row)
            for row in self.conn.execute("SELECT * FROM session_parts ORDER BY plan_item_id")
        ]
        value = proposal(
            {"action": "move_session", "item": "p1", "to": "2026-11-12"},
            {
                "action": "change_session",
                "item": "p1",
                "label": "Changed",
                "parts": [{"part_kind": "set", "reps": 5, "weight_kg": 40}],
            },
            version=1,
        )
        value["why"] = "New explanation"
        self.save(value, "save-2")
        self.undo(2)
        restored = dict(self.conn.execute("SELECT * FROM plan_items").fetchone())
        self.assertEqual(restored, {**original, "version": 3, "plan_version": 3})
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute("SELECT * FROM session_parts ORDER BY plan_item_id")
            ],
            original_parts,
        )
        inverse_before = json.loads(
            self.conn.execute("SELECT before_json FROM journal WHERE version=3").fetchone()[0]
        )
        self.assertEqual(inverse_before["p1"]["label"], "Changed")
        self.assertEqual(inverse_before["p1"]["parts"][0]["weight_kg"], 40)

    def test_undo_completion_and_removal_and_refuse_stale(self) -> None:
        self.save(proposal(ADD, {**ADD, "item": "p2"}))
        self.save(
            proposal(
                {"action": "mark_done", "item": "p1"},
                {"action": "remove_session", "item": "p2"},
                version=1,
            ),
            "save-2",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.undo(1)
        self.undo(2)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT status FROM plan_items")],
            ["saved", "saved"],
        )
        self.assertEqual(self.counts(), (3, 2, 2))

    def test_undo_write_failure_rolls_back_and_delivery_state_is_refused(self) -> None:
        self.save(proposal(ADD))
        self.conn.executescript(
            "CREATE TRIGGER refuse_restore BEFORE UPDATE ON plan_items BEGIN SELECT RAISE(ABORT, 'invented failure'); END;"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invented failure"):
            self.undo(1)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "saved")
        self.conn.execute("DROP TRIGGER refuse_restore")
        self.conn.execute("UPDATE plan_items SET status='sent'")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.undo(1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_undo_refuses_unsupported_journal_operation(self) -> None:
        self.conn.execute(
            "INSERT INTO journal (request_id, proposal_json, before_json, approved_by, saved_at) VALUES ('future', ?, '{}', 'chat', '2026-11-07T00:00:00Z')",
            (json.dumps({"actions": [{"action": "set_goal"}]}),),
        )
        with self.assertRaisesRegex(ValueError, "does not support"):
            self.undo(1)
        self.assertEqual(self.counts(), (1, 0, 0))
