import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_schema import insert
from tfs.mcp.tool_access import open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.proposals.apply import preview, save

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
        self.assertEqual(
            json.loads(row["before_json"]), {"items": {"p1": None}, "goals": {}, "limits": {}}
        )
        self.assertEqual(row["saved_at"], "2026-11-06T23:00:00Z")

    def test_start_time_add_move_clear_and_retry(self) -> None:
        first = proposal({**ADD, "start_hint": "07:30"})
        self.assertEqual(preview(self.path, first, now=NOW)["items"][0]["start_hint"], "07:30")
        self.save(first)
        moved = proposal({"action": "move_session", "item": "p1", "to": "2026-11-10"}, version=1)
        self.save(moved, "move")
        self.assertEqual(
            self.conn.execute("SELECT start_hint FROM plan_items").fetchone()[0], "07:30"
        )
        changed = proposal(
            {"action": "change_session", "item": "p1", "start_hint": "23:59"}, version=2
        )
        receipt = self.save(changed, "time")
        self.assertEqual(self.save(changed, "time"), receipt)
        self.assertEqual(
            self.conn.execute("SELECT start_hint FROM plan_items").fetchone()[0], "23:59"
        )
        self.save(
            proposal({"action": "change_session", "item": "p1", "start_hint": None}, version=3),
            "clear",
        )
        self.assertIsNone(self.conn.execute("SELECT start_hint FROM plan_items").fetchone()[0])
        self.save(
            proposal(
                {"action": "move_session", "item": "p1", "to": "2026-11-11", "start_hint": "00:00"},
                version=4,
            ),
            "midnight",
        )
        self.assertEqual(
            tuple(self.conn.execute("SELECT local_date, start_hint FROM plan_items").fetchone()),
            ("2026-11-11", "00:00"),
        )

    def test_invalid_later_action_and_unknown_fields_save_nothing(self) -> None:
        cases = [
            proposal(ADD, {"action": "move_session", "item": "missing", "to": "2026-11-09"}),
            proposal({**ADD, "send_now": True}),
            proposal({**ADD, "action": "erase_session"}),
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
        self.assertEqual(json.loads(row[0])["items"]["p1"]["local_date"], "2026-11-08")

    def test_approval_route_and_aware_time_are_required(self) -> None:
        for route, now in [("automatic", NOW), ("chat", NOW.replace(tzinfo=None))]:
            with self.subTest(route=route), self.assertRaises(ValueError):
                save(self.path, proposal(ADD), request_id="s1", approved_by=route, now=now)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_goal_limit_and_session_save_together(self) -> None:
        goal = {
            "action": "set_goal",
            "item": "p1",
            "type": "marathon",
            "date": "2027-04-01",
            "target": {"description": "Finish comfortably", "value": 14400, "unit": "s"},
        }
        limit = {
            "action": "set_limit",
            "item": "p1",
            "kind": "max_session_length",
            "values": {"minutes": 60},
        }
        value = proposal(goal, limit, {**ADD, "goal_ids": ["p1"]})
        planned = preview(self.path, value, now=NOW)
        self.assertEqual(planned["goals"][0]["target"], goal["target"])
        self.assertEqual(planned["limits"][0]["values"], {"minutes": 60})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM goals").fetchone()[0], 0)
        self.assertEqual(self.save(value)["plan_version"], 1)
        self.assertEqual(self.save(value)["plan_version"], 1)
        self.assertEqual(
            json.loads(self.conn.execute("SELECT goal_ids_json FROM plan_items").fetchone()[0]),
            ["p1"],
        )
        self.assertEqual(
            tuple(self.conn.execute("SELECT type, local_date, target_json FROM goals").fetchone()),
            (
                "marathon",
                "2027-04-01",
                '{"description":"Finish comfortably","unit":"s","value":14400}',
            ),
        )
        self.assertEqual(self.conn.execute("SELECT format_version FROM journal").fetchone()[0], 5)
        self.assertEqual(
            json.loads(self.conn.execute("SELECT target_json FROM goals").fetchone()[0]),
            goal["target"],
        )
        self.assertEqual(
            json.loads(self.conn.execute("SELECT values_json FROM limits").fetchone()[0]),
            {"minutes": 60},
        )
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "saved")

    def test_malformed_goals_limits_and_forward_reference_save_nothing(self) -> None:
        goal = {
            "action": "set_goal",
            "item": "g1",
            "type": "custom",
            "date": "2027-04-01",
            "target": {"description": "A goal"},
        }
        limit = {
            "action": "set_limit",
            "item": "l1",
            "kind": "max_session_length",
            "values": {"minutes": 60},
        }
        bad = [
            {**goal, "type": "unknown"},
            {**goal, "date": "2026-11-07"},
            {**goal, "target": {"description": "Goal", "value": 5}},
            {**goal, "target": {"description": "Goal", "value": True, "unit": "s"}},
            {**goal, "target": {"description": "Goal", "dose": 5}},
            {**limit, "kind": "unknown"},
            {**limit, "values": {"minutes": 0}},
            {**limit, "values": {"minutes": float("inf")}},
            {**limit, "kind": "injury", "values": {"description": 0}},
            {**limit, "kind": "unavailable_day", "values": {"date": "2026-11-06"}},
        ]
        for action in bad:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.save(proposal(goal, action))
            self.assertEqual(self.counts(), (0, 0, 0))
            self.assertEqual(self.conn.execute("SELECT count(*) FROM goals").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM limits").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "Unknown goal"):
            self.save(proposal({**ADD, "goal_ids": ["g1"]}, goal))

    def test_limit_only_save_invalidates_proposal(self) -> None:
        self.save(proposal(ADD))
        old = proposal({"action": "move_session", "item": "p1", "to": "2026-11-10"}, version=1)
        preview(self.path, old, now=NOW)
        self.save(
            proposal(
                {
                    "action": "set_limit",
                    "item": "l1",
                    "kind": "max_session_length",
                    "values": {"minutes": 30},
                },
                version=1,
            ),
            "limit",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.save(old, "old")

    def forecasts(self) -> list:
        reader = open_reader(self.path)
        self.addCleanup(reader.close)
        return query(
            reader,
            "SELECT goal_id, low, high, unit, method, local_date, label FROM coach_v1_forecasts",
        )["rows"]

    def goal(self, kind: str) -> dict:
        return {
            "action": "set_goal",
            "item": "g1",
            "type": kind,
            "date": "2027-04-01",
            "target": {"description": "Finish"},
        }

    def forecast(self, low: int, high: int) -> dict:
        return {
            "action": "set_forecast",
            "item": "g1",
            "low": low,
            "high": high,
            "unit": "minutes",
            "method": "Scale recent station times",
        }

    def test_a_coach_forecast_shows_and_the_newest_replaces_it(self) -> None:
        # GIVEN a Hyrox goal
        self.save(proposal(self.goal("hyrox")))
        source = [{"url": "https://example.com/pacing", "type": "article"}]
        # WHEN Save gets a range of 85 to 95 minutes with a method and one source
        self.save({**proposal(self.forecast(85, 95), version=1), "sources": source}, "first")
        # THEN the view shows it with today's date
        self.assertEqual(
            self.forecasts(),
            [
                [
                    "g1",
                    85,
                    95,
                    "minutes",
                    "Scale recent station times",
                    "2026-11-07",
                    "Coach estimate",
                ]
            ],
        )
        # WHEN a second Save arrives
        self.save(proposal(self.forecast(80, 90), version=2), "second")
        # THEN only the newest estimate shows
        self.assertEqual([row[1:3] for row in self.forecasts()], [[80, 90]])

    def test_a_goal_with_a_code_forecast_refuses_a_coach_forecast(self) -> None:
        # GIVEN a marathon goal
        self.save(proposal(self.goal("marathon")))
        # WHEN Save gets set_forecast THEN it refuses
        with self.assertRaisesRegex(ValueError, "this goal has a code forecast"):
            self.save(proposal(self.forecast(85, 95), version=1), "forecast")
        self.assertEqual(self.forecasts(), [])

    def test_an_unknown_goal_or_backwards_range_is_refused(self) -> None:
        # GIVEN no goal
        # WHEN Save gets a forecast for it THEN it refuses
        with self.assertRaisesRegex(ValueError, "Unknown goal"):
            self.save(proposal(self.forecast(85, 95)), "unknown")
        # GIVEN a Hyrox goal WHEN the range runs backwards THEN it refuses
        self.save(proposal(self.goal("hyrox")), "goal")
        with self.assertRaises(ValueError):
            self.save(proposal(self.forecast(95, 85), version=1), "backwards")
