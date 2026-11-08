import copy
import io
import json
import unittest
from contextlib import closing
from datetime import UTC, timedelta, timezone
from pathlib import Path

from tests.helpers import insert_file, open_database
from tests.test_proposals import ADD, NOW, proposal
from tests.test_records import SESSION
from tfs.importer.raw_store import save_raw
from tfs.importer.records import store
from tfs.importer.session import process_pending, rebuild_all, rebuild_pending
from tfs.mcp.tool_access import open_reader, query
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import transaction
from tfs.proposals import matching
from tfs.proposals.apply import preview, save, undo


class PlanMatchingTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(self)
        self.path = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
        self.arrival = 0

    def plan(self, item="p1", hint=None, family="strength", day="2026-11-08"):
        return {**ADD, "item": item, "start_hint": hint, "family": family, "date": day}

    def save(self, *actions):
        version = self.conn.execute("SELECT coalesce(max(version),0) FROM journal").fetchone()[0]
        return save(
            self.path,
            proposal(*actions, version=version),
            request_id=f"save-{version+1}",
            approved_by="chat",
            now=NOW,
        )

    def import_records(self, records, connector="invented", rebuild=True, tz=None):
        self.arrival += 1
        sha = insert_file(self.conn, self.arrival, "2026-12-01T00:00:00Z", connector)
        self.conn.execute(
            "INSERT OR IGNORE INTO sources(connector, last_received_at) VALUES (?, '2026-12-01T00:00:00Z')",
            (connector,),
        )
        result = store(
            self.conn,
            connector=connector,
            file_sha256=sha,
            arrival_seq=self.arrival,
            received_at="2026-12-01T00:00:00Z",
            reader_version="r1",
            records=records,
            tz=tz,
        )
        self.assertEqual(result.rejected, {})
        if rebuild:
            rebuild_pending(self.conn, tz=tz)

    def workout(self, key="w1", start="2026-11-08T07:35:00Z", **changes):
        row = {
            **copy.deepcopy(SESSION),
            "key": key,
            "start_utc": start,
            "local_date": "2026-11-08",
            "utc_offset_min": 0,
        }
        return row | changes

    def matches(self):
        return [
            tuple(r)
            for r in self.conn.execute(
                "SELECT item_id, group_id, reason FROM plan_matches ORDER BY item_id"
            )
        ]

    def visible(self):
        with closing(open_reader(self.path)) as reader:
            result = query(
                reader,
                "SELECT plan_item_id,status,matched_workout_id,match_reason FROM coach_v1_plan_items ORDER BY plan_item_id",
            )
            self.assertNotIn("error", result)
            return result["rows"]

    def unlink(self, item="p1"):
        with closing(open_reader(self.path)) as reader:
            rows = query(
                reader, "SELECT plan_item_id,matched_record_ids_json FROM coach_v1_plan_items"
            )["rows"]
        records = next(json.loads(row[1]) for row in rows if row[0] == item)
        return {"action": "unlink_plan_match", "item": item, "record_ids": records}

    def test_unlink_duplicate_match_preview_and_undo_preserve_delivery_history(self):
        self.save(self.plan())
        self.link("p1", "invented", "routine-1")
        self.import_records([self.workout(plan_ref="routine-1")])
        self.import_records([self.workout()], connector="other")
        action = self.unlink()
        self.assertEqual(len(action["record_ids"]), 2)
        value = proposal(action, version=1)
        shown = preview(self.path, value, now=NOW)
        self.assertEqual(
            shown["match_exclusions"], [{"item": "p1", "record_ids": action["record_ids"]}]
        )
        self.assertEqual(shown["delivery_targets"], {})
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM plan_match_exclusions").fetchone()[0], 0
        )
        history = [tuple(row) for row in self.conn.execute("SELECT * FROM jobs")]
        self.save(action)
        self.assertEqual(self.visible(), [["p1", "saved", None, None]])
        self.assertEqual(matching.exclusions(self.conn, "p1"), action["record_ids"])
        undo(self.path, 2, request_id="undo-unlink", approved_by="chat", now=NOW)
        self.assertEqual(self.visible()[0][1], "done")
        undo(self.path, 3, request_id="redo-unlink", approved_by="chat", now=NOW)
        self.assertEqual(self.visible(), [["p1", "saved", None, None]])
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM jobs")], history)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM remote_links").fetchone()[0], 1)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT format_version FROM journal")],
            [2, 4, 4, 4],
        )

    def test_changed_match_refuses_unlink_without_partial_write(self):
        self.save(self.plan())
        self.import_records([self.workout()])
        value = proposal(self.unlink(), version=1)
        preview(self.path, value, now=NOW)
        self.import_records([self.workout()], connector="other")
        with self.assertRaisesRegex(ValueError, "match changed"):
            save(self.path, value, request_id="stale-match", approved_by="chat", now=NOW)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 1)
        self.assertEqual(matching.exclusions(self.conn, "p1"), [])
        self.assertEqual(self.visible()[0][1], "done")

    def test_unlink_then_edit_obeys_action_order_and_retries_once(self):
        self.save(self.plan())
        self.import_records([self.workout()])
        action = self.unlink()
        edit = {"action": "move_session", "item": "p1", "to": "2026-11-09"}
        with self.assertRaisesRegex(ValueError, "done"):
            self.save(edit, action)
        value = proposal(action, edit, version=1)
        for _ in range(2):
            save(self.path, value, request_id="unlink-edit", approved_by="chat", now=NOW)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute("SELECT local_date FROM plan_items").fetchone()[0], "2026-11-09"
        )
        self.assertEqual(self.matches(), [])
        undo(self.path, 2, request_id="undo-edit", approved_by="chat", now=NOW)
        self.assertEqual(self.visible()[0][1], "done")
        self.assertEqual(matching.exclusions(self.conn, "p1"), [])

    def test_nearest_time_duplicate_group_and_rebuild_are_deterministic(self):
        self.save(self.plan("early", "07:30"), self.plan("late", "17:30"))
        self.import_records([self.workout(), self.workout("w2", "2026-11-08T17:35:00Z")])
        self.import_records([self.workout()], connector="other")
        self.assertEqual([r[:2] for r in self.visible()], [["early", "done"], ["late", "done"]])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_groups").fetchone()[0], 2)
        self.assertEqual([r[2] for r in self.matches()], ["date_family", "date_family"])
        before = self.matches()
        self.conn.execute("INSERT OR IGNORE INTO rebuild_dates VALUES ('2026-11-08')")
        rebuild_pending(self.conn)
        self.assertEqual(self.matches(), before)
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT status FROM plan_items")], ["saved", "saved"]
        )

    def link(self, item, connector, remote_id):
        state = dict(self.conn.execute("SELECT * FROM plan_items WHERE id=?", (item,)).fetchone())
        key = f"link-{item}-{connector}"
        self.conn.execute(
            "INSERT INTO jobs(id,item_id,plan_version,connector,payload_json,status,next_attempt_at,created_at) VALUES(?,?,?,?,?,'sent',?,?)",
            (
                key,
                item,
                state["plan_version"],
                connector,
                json.dumps(state),
                "2026-11-07T00:00:00Z",
                "2026-11-07T00:00:00Z",
            ),
        )
        self.conn.execute(
            "INSERT INTO remote_links VALUES(?,?,?,?,?)",
            (item, connector, remote_id, "version-1", key),
        )

    def test_reference_is_connector_qualified_and_wins_over_date_and_family(self):
        self.save(
            self.plan("fallback", "07:30"), self.plan("linked", family="run", day="2026-11-09")
        )
        self.link("linked", "provider", "routine-1")
        self.import_records([self.workout(plan_ref="routine-1")], connector="wrong-provider")
        self.assertEqual([r[0] for r in self.matches()], ["fallback"])
        self.import_records([self.workout(plan_ref="routine-1")], connector="provider")
        self.assertEqual([(r[0], r[2]) for r in self.matches()], [("linked", "reference")])
        self.assertEqual(
            [r[:2] for r in self.visible()], [["fallback", "saved"], ["linked", "done"]]
        )

    def test_pending_delete_hides_match_and_manual_done_survives(self):
        self.save(self.plan())
        self.import_records([self.workout()])
        with self.assertRaisesRegex(ValueError, "done"):
            self.save({"action": "move_session", "item": "p1", "to": "2026-11-09"})
        self.save({"action": "mark_done", "item": "p1"})
        self.import_records(
            [
                {
                    "kind": "deleted",
                    "stream": "workouts",
                    "key": "w1",
                    "revision_at": "2027-01-01T00:00:00.000Z",
                }
            ],
            rebuild=False,
        )
        self.assertEqual(self.visible(), [["p1", "done", None, None]])
        rebuild_pending(self.conn)
        self.assertEqual(self.matches(), [])
        self.assertEqual(self.visible(), [["p1", "done", None, None]])

    def test_correction_removes_automatic_completion_and_replay_agrees(self):
        self.save(self.plan())
        self.import_records([self.workout()])
        self.import_records(
            [self.workout(family="run", revision_at="2027-01-01T00:00:00.000Z")], rebuild=False
        )
        self.assertEqual(self.visible(), [["p1", "saved", None, None]])
        rebuild_pending(self.conn)
        self.assertEqual(self.matches(), [])
        self.import_records([self.workout()])
        self.assertEqual(self.matches(), [])

    def test_save_undo_matches_existing_workout_and_ties_use_item_id(self):
        self.import_records([self.workout()])
        self.save(self.plan("b"), self.plan("a"))
        self.assertEqual([r[0] for r in self.matches()], ["a"])
        undo(self.path, 1, request_id="undo", approved_by="chat", now=NOW)
        self.assertEqual(self.matches(), [])
        undo(self.path, 2, request_id="redo", approved_by="chat", now=NOW)
        self.assertEqual([r[0] for r in self.matches()], ["a"])

    def test_offsets_and_utc_only_local_zone_choose_nearest_time(self):
        self.save(self.plan("early", "07:30"), self.plan("late", "17:30"))
        self.import_records([self.workout(start="2026-11-08T05:35:00Z", utc_offset_min=720)])
        self.assertEqual([r[0] for r in self.matches()], ["late"])
        self.import_records(
            [
                self.workout(
                    start="2026-11-08T05:35:00Z",
                    utc_offset_min=None,
                    revision_at="2027-01-01T00:00:00.000Z",
                )
            ],
            tz=timezone(timedelta(hours=2)),
        )
        self.assertEqual([r[0] for r in self.matches()], ["early"])
        with transaction(self.conn):
            matching.rebuild(self.conn, tz=timezone(timedelta(hours=2)))
        self.assertEqual([r[0] for r in self.matches()], ["early"])

    def test_raw_replay_preserves_matches_manual_done_and_journal(self):
        self.save(self.plan(hint="18:00"))
        fixture = Path(__file__).parent / "fixtures/hevy/workouts_page.json"
        data = fixture.read_text().replace("2026-10-20", "2026-11-08").encode()
        save_raw(
            self.conn,
            self.path.parent,
            "hevy",
            "workouts_page",
            "json",
            io.BytesIO(data),
            now="2026-12-01T00:00:00Z",
        )
        results = process_pending(self.conn, self.path.parent, tz=UTC)
        self.assertEqual([r.status for r in results], ["ok"])
        self.assertEqual(len(self.matches()), 1)
        expected = self.matches()
        self.conn.execute("DROP TRIGGER jobs_resolution_immutable")
        self.conn.execute("ALTER TABLE jobs DROP COLUMN resolved_by_version")
        self.conn.execute("DROP TABLE plan_match_exclusions")
        self.conn.execute("DROP TABLE plan_matches")
        self.conn.execute("PRAGMA user_version=4")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM rebuild_dates").fetchone()[0], 0)
        self.assertEqual(migrate(self.conn), 7)
        self.assertEqual(process_pending(self.conn, self.path.parent, tz=UTC), [])
        self.assertEqual(self.matches(), expected)
        self.save({"action": "mark_done", "item": "p1"})
        before = self.matches()
        journal = [tuple(r) for r in self.conn.execute("SELECT * FROM journal")]
        self.assertEqual(rebuild_all(self.conn, self.path.parent, tz=UTC), 1)
        self.assertEqual(self.matches(), before)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM journal")], journal)
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "done")

        action = self.unlink()
        self.save(action)
        self.assertEqual(self.visible(), [["p1", "done", None, None]])
        before_unlink = [tuple(r) for r in self.conn.execute("SELECT * FROM journal")]
        self.assertEqual(rebuild_all(self.conn, self.path.parent, tz=UTC), 1)
        self.assertEqual(self.visible(), [["p1", "done", None, None]])
        self.assertEqual(matching.exclusions(self.conn, "p1"), action["record_ids"])
        self.assertEqual(
            [tuple(r) for r in self.conn.execute("SELECT * FROM journal")], before_unlink
        )
