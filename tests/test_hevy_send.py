import copy
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from tests.helpers import open_database
from tests.test_proposals import ADD, NOW, proposal
from tfs.connectors.core import send
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.connectors.hevy import routines
from tfs.mcp.tool_access import open_reader, query
from tfs.proposals.apply import preview, save
from tfs.scheduling import jobs

ITEM = {
    "family": "strength",
    "status": "saved",
    "local_date": "2026-11-08",
    "label": "Push café ✓",
    "parts": [{"part_kind": "set", "label": "Press", "reps": 8, "weight_kg": 30.125}],
}
VERSION = "2026-11-08T00:00:00Z"
NAMED = {
    "id": "routine-r",
    "title": "Leg day",
    "folder_id": 7,
    "updated_at": "2026-11-01T00:00:00Z",
    "exercises": [
        {
            "exercise_template_id": "squat",
            "superset_id": None,
            "rest_seconds": 90,
            "notes": "Go deep",
            "sets": [{"type": "normal", "weight_kg": 60, "reps": 5}],
        }
    ],
}


class FakeHevy:
    def __init__(self):
        self.templates = [{"id": "press", "title": "Press"}, {"id": "squat", "title": "Squat"}]
        self.saved = {}
        self.calls = []
        self.failure = None
        self.lose_ack = False

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path.removeprefix("/v1")
        self.calls.append((method, path, headers, body))
        if self.failure and (method, path) == self.failure[:2]:
            result = self.failure[2]
            if isinstance(result, Exception):
                raise result
            return result, {}, b'{"message":"invented-key"}'
        if path == "/exercise_templates":
            value = {"page_count": 1, "exercise_templates": self.templates}
        elif method == "GET" and path == "/routines":
            value = {"page_count": 1 if self.saved else 0, "routines": list(self.saved.values())}
        elif method == "GET":
            routine = self.saved.get(path.rsplit("/", 1)[1])
            if routine is None:
                return 404, {}, b'{"error":"not-found"}'
            value = {"routine": routine}
        else:
            key = "routine-1" if method == "POST" else path.rsplit("/", 1)[1]
            self.saved[key] = {**json.loads(body)["routine"], "id": key, "updated_at": VERSION}
            if self.lose_ack:
                self.lose_ack = False
                raise TimeoutError("invented-key")
            value = {"routine": self.saved[key]}
        return 201 if method == "POST" else 200, {}, json.dumps(value).encode()

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PUT")]


class HevySendTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHevy()

    def send(self, item=None, **changes):
        payload = {
            "item": copy.deepcopy(item or ITEM),
            "op_id": "operation-1",
            "remote_id": None,
            "remote_version": None,
            "in_doubt": False,
            "secrets": {"api_key": "invented-key"},
            "now": VERSION,
            "http": self.http,
        }
        return routines.send(**(payload | changes))

    def plan_with_hevy(self):
        conn = open_database(self)
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        conn.execute("INSERT INTO sources(connector) VALUES ('hevy')")
        conn.execute(
            "INSERT INTO app_targets(connector, remote_id, title, folder, remote_version)"
            " VALUES ('hevy', ?, ?, ?, ?)",
            (NAMED["id"], NAMED["title"], NAMED["folder_id"], NAMED["updated_at"]),
        )
        self.http.saved[NAMED["id"]] = copy.deepcopy(NAMED)

        def deliver(connector, operation, payload, **options):
            return routines.send(**payload, http=self.http)

        for patch in (
            mock.patch.object(send.password_store, "get", return_value="invented-key"),
            mock.patch.object(send.runner, "run", side_effect=deliver),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return conn, path

    def approve(self, path, *actions, version=0):
        value = proposal(*actions, version=version)
        shown = preview(path, value, now=NOW)
        value["delivery_targets"] = shown["delivery_targets"]
        save(path, value, request_id=f"save-{version}", approved_by="chat", now=NOW)
        return shown

    def plan_status(self, path):
        with closing(open_reader(path)) as reader:
            sql = "SELECT plan_item_id, status FROM coach_v1_plan_items ORDER BY plan_item_id"
            return query(reader, sql)["rows"]

    def test_with_hevy_connected_a_strength_save_queues_a_hevy_job_that_sends(self):
        # GIVEN Hevy is connected
        conn, path = self.plan_with_hevy()
        # WHEN a strength session is approved
        shown = self.approve(path, ADD)
        # THEN a Hevy job is queued and the worker sends it
        self.assertEqual(shown["delivery_targets"], {"p1": ["hevy"]})
        self.assertTrue(jobs.run_one(path, to_utc_text(NOW)))
        self.assertEqual(self.plan_status(path), [["p1", "sent"]])

    def test_preview_shows_the_title_of_the_named_routine(self):
        # GIVEN Hevy is connected and TFS has read the person's routines
        _, path = self.plan_with_hevy()
        # WHEN a session that names a routine is previewed
        shown = preview(path, proposal({**ADD, "send_into": {"hevy": NAMED["id"]}}), now=NOW)
        # THEN the preview shows that routine's title
        self.assertEqual(shown["send_into_titles"], {"p1": {"hevy": NAMED["title"]}})

    def test_approved_session_goes_into_the_named_routine(self):
        # GIVEN an approved session that names a routine
        _, path = self.plan_with_hevy()
        self.approve(path, {**ADD, "send_into": {"hevy": NAMED["id"]}})
        # WHEN the worker sends it
        self.assertTrue(jobs.run_one(path, to_utc_text(NOW)))
        # THEN the named routine holds the session and no routine is created
        stored = self.http.saved[NAMED["id"]]
        self.assertEqual([e["exercise_template_id"] for e in stored["exercises"]], ["press"])
        self.assertEqual(list(self.http.saved), [NAMED["id"]])
        self.assertEqual(self.plan_status(path), [["p1", "sent"]])

    def test_removing_a_session_sent_to_a_named_routine_queues_nothing(self):
        # GIVEN a session sent into a named routine
        conn, path = self.plan_with_hevy()
        self.approve(path, {**ADD, "send_into": {"hevy": NAMED["id"]}})
        jobs.run_one(path, to_utc_text(NOW))
        stored = copy.deepcopy(self.http.saved[NAMED["id"]])
        # WHEN the session is removed
        shown = self.approve(path, {"action": "remove_session", "item": "p1"}, version=1)
        # THEN nothing is queued and the routine stays as it is
        self.assertEqual(shown["delivery_targets"], {})
        self.assertFalse(jobs.run_one(path, to_utc_text(NOW)))
        self.assertEqual(self.http.saved[NAMED["id"]], stored)

    def test_second_session_on_the_same_routine_unlinks_the_first(self):
        # GIVEN a session sent into a named routine
        _, path = self.plan_with_hevy()
        self.approve(path, {**ADD, "send_into": {"hevy": NAMED["id"]}})
        jobs.run_one(path, to_utc_text(NOW))
        # WHEN a second session names the same routine and is sent
        second = {**ADD, "item": "p2", "date": "2026-11-09", "send_into": {"hevy": NAMED["id"]}}
        self.approve(path, second, version=1)
        self.assertTrue(jobs.run_one(path, to_utc_text(NOW)))
        # THEN the first shows as not sent, and it can still be changed
        self.assertEqual(self.plan_status(path), [["p1", "saved"], ["p2", "sent"]])
        self.approve(path, {"action": "change_session", "item": "p1", "label": "Pull"}, version=2)

    def test_removed_routine_returns_manual_instruction_without_http(self):
        result = self.send(item={**ITEM, "status": "removed"}, remote_id="routine-1")
        self.assertEqual(
            (result["status"], result["manual_action"], result["remote_id"]),
            ("needs_you", "delete", "routine-1"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertIn("cannot delete", result["message"])

    def test_mapping_failure_can_be_corrected_before_one_send(self):
        conn = open_database(self)
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        conn.execute("INSERT INTO sources(connector) VALUES ('hevy')")
        manifests = load_manifests()
        manifests["hevy"]["sends"] = ["strength"]
        stamp = to_utc_text(NOW)

        def deliver(connector, operation, payload, **options):
            self.assertEqual((connector, operation), ("hevy", "send"))
            return routines.send(**payload, http=self.http)

        def approved(value, request):
            value["delivery_targets"] = preview(path, value, now=NOW)["delivery_targets"]
            return save(path, value, request_id=request, approved_by="chat", now=NOW)

        with (
            mock.patch.object(jobs, "load_manifests", return_value=manifests),
            mock.patch.object(send.password_store, "get", return_value="invented-key"),
            mock.patch.object(send.runner, "run", side_effect=deliver),
        ):
            bad = {**ADD, "parts": [{**ITEM["parts"][0], "label": "Not in catalog"}]}
            approved(proposal(bad), "bad-name")
            self.assertTrue(jobs.run_one(path, stamp))
            failed = dict(conn.execute("SELECT * FROM jobs").fetchone())
            self.assertEqual(failed["status"], "needs_you")
            self.assertIs(json.loads(failed["result_json"])["safe_to_replace"], True)
            self.assertFalse(jobs.run_one(path, stamp))
            self.assertEqual(self.http.writes(), [])
            with closing(open_reader(path)) as reader:
                self.assertEqual(
                    query(reader, "SELECT safe_to_replace FROM coach_v1_deliveries")["rows"], [[1]]
                )
            approved(
                proposal(
                    {"action": "change_session", "item": "p1", "parts": ITEM["parts"]}, version=1
                ),
                "correct-name",
            )
            replacement = dict(conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone())
            self.assertNotEqual(failed["id"], replacement["id"])
            self.assertTrue(jobs.run_one(path, stamp))
            self.assertEqual(len(self.http.writes()), 1)
            self.assertEqual(conn.execute("SELECT status FROM plan_items").fetchone()[0], "sent")
            self.assertEqual(
                dict(conn.execute("SELECT * FROM jobs WHERE id=?", (failed["id"],)).fetchone()),
                {**failed, "status": "cancelled"},
            )

    def test_create_preserves_exact_targets_and_adjacent_exercise_order(self):
        part = ITEM["parts"][0]
        item = {
            **ITEM,
            "parts": [
                part,
                {**part, "part_kind": "warmup", "weight_kg": 0},
                {**part, "label": "Squat"},
                part,
            ],
        }
        self.assertEqual(
            self.send(item), {"status": "sent", "remote_id": "routine-1", "remote_version": VERSION}
        )
        writes = self.http.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("POST", "/routines"))
        value = json.loads(writes[0][3])["routine"]
        self.assertEqual(value["title"], "2026-11-08 Push café ✓")
        self.assertEqual(
            [exercise["notes"] for exercise in value["exercises"]],
            ["[TFS:operation-1]", None, None],
        )
        self.assertIsNone(value["folder_id"])
        self.assertEqual(
            [exercise["exercise_template_id"] for exercise in value["exercises"]],
            ["press", "squat", "press"],
        )
        sets = value["exercises"][0]["sets"]
        self.assertEqual(
            [(entry["type"], entry["weight_kg"], entry["reps"]) for entry in sets],
            [("normal", 30.125, 8), ("warmup", 0, 8)],
        )
        self.assertIsNone(sets[0]["distance_meters"])
        self.assertEqual(writes[0][2]["api-key"], "invented-key")
        self.assertEqual(self.http.calls[-1][:2], ("GET", "/routines/routine-1"))

    def named(self, **changes):
        self.http.saved[NAMED["id"]] = {**copy.deepcopy(NAMED), **changes}
        return {"remote_id": NAMED["id"], "remote_version": NAMED["updated_at"]}

    def test_named_routine_holds_the_session_and_keeps_its_title_and_folder(self):
        # GIVEN an approved session that names routine R
        args = self.named()
        # WHEN TFS sends it
        result = self.send(**args)
        # THEN R holds the session, keeps its title and folder, and no routine is created
        self.assertEqual(
            result, {"status": "sent", "remote_id": NAMED["id"], "remote_version": VERSION}
        )
        stored = self.http.saved[NAMED["id"]]
        self.assertEqual(
            (stored["title"], stored["folder_id"]), (NAMED["title"], NAMED["folder_id"])
        )
        self.assertEqual([e["exercise_template_id"] for e in stored["exercises"]], ["press"])
        self.assertEqual(list(self.http.saved), [NAMED["id"]])
        self.assertEqual([call[0] for call in self.http.writes()], ["PUT"])

    def test_lost_reply_from_named_routine_is_confirmed_by_its_tag(self):
        # GIVEN a send to R whose reply was lost
        args = self.named()
        self.http.lose_ack = True
        self.assertEqual(self.send(**args)["status"], "unknown")
        # WHEN TFS retries
        result = self.send(**args, in_doubt=True)
        # THEN it finds the tag in R and writes nothing
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(self.http.writes()), 1)

    def test_named_routine_changed_in_the_app_or_deleted_is_not_written(self):
        for changes, status in (({"updated_at": VERSION}, "conflict"), (None, "needs_you")):
            with self.subTest(changes=changes):
                # GIVEN R changed in the app since TFS read it, or R was deleted
                self.http = FakeHevy()
                args = self.named(**changes) if changes else {**self.named(), "remote_id": "gone"}
                # WHEN TFS sends
                result = self.send(**args)
                # THEN nothing is written
                self.assertEqual(result["status"], status)
                self.assertEqual(self.http.writes(), [])
                self.assertNotIn("auth_failed", result)

    def test_lost_ack_reconciles_existing_marker_without_second_write(self):
        self.http.lose_ack = True
        self.assertEqual(self.send()["status"], "unknown")
        result = self.send(in_doubt=True)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(self.http.writes()), 1)
        self.assertEqual(self.send()["status"], "sent")
        self.assertEqual(len(self.http.writes()), 1)

    def test_absent_duplicate_or_changed_marker_never_repeats_a_write(self):
        self.assertEqual(self.send(in_doubt=True)["status"], "unknown")
        self.assertEqual(self.http.writes(), [])
        self.send()
        original = copy.deepcopy(self.http.saved["routine-1"])
        self.http.saved["duplicate"] = {**original, "id": "duplicate"}
        self.assertEqual(self.send(in_doubt=True)["status"], "needs_you")
        del self.http.saved["duplicate"]
        self.http.saved["routine-1"]["exercises"][0]["sets"][0]["reps"] = 99
        self.assertEqual(self.send(in_doubt=True)["status"], "conflict")
        self.assertEqual(len(self.http.writes()), 1)

    def test_update_checks_version_and_preserves_folder_and_notes(self):
        self.send()
        self.http.saved["routine-1"].update(folder_id=7, notes="Keep this note")
        args = {"remote_id": "routine-1", "remote_version": "old-version", "op_id": "operation-2"}
        self.assertEqual(self.send(**args)["status"], "conflict")
        self.assertEqual(len(self.http.writes()), 1)
        args["remote_version"] = VERSION
        self.assertEqual(self.send(**args, in_doubt=True)["status"], "unknown")
        self.assertEqual(self.send(**args)["status"], "sent")
        written = json.loads(self.http.writes()[-1][3])["routine"]
        self.assertEqual((written["folder_id"], written["notes"]), (7, "Keep this note"))
        self.assertEqual(self.http.writes()[-1][:2], ("PUT", "/routines/routine-1"))
        self.assertEqual(self.send(**args, in_doubt=True)["status"], "sent")
        self.assertEqual(len(self.http.writes()), 2)

    def test_unknown_ambiguous_and_unsupported_targets_are_not_guessed(self):
        for changes in (
            {"label": "Not in catalog"},
            {"avg_hr": 120},
            {"distance_m": 6.001},
            {"duration_s": 1.25},
            {"extras_json": '{"rest_s":90}'},
            {"part_kind": "lap"},
        ):
            with self.subTest(changes=changes):
                item = {**ITEM, "parts": [{**ITEM["parts"][0], **changes}]}
                result = self.send(item)
                self.assertEqual(result["status"], "needs_you")
                self.assertIs(result["safe_to_replace"], True)
                self.assertNotIn("safe_to_replace", self.send(item, in_doubt=True))
        self.http.templates.append({"id": "other-press", "title": " PRESS "})
        self.assertEqual(self.send()["status"], "needs_you")
        self.assertEqual(self.http.writes(), [])

    def test_whole_metres_and_seconds_are_preserved_without_rounding(self):
        item = {**ITEM, "parts": [{**ITEM["parts"][0], "distance_m": 6001.0, "duration_s": 61.0}]}
        self.assertEqual(self.send(item)["status"], "sent")
        entry = self.http.saved["routine-1"]["exercises"][0]["sets"][0]
        self.assertEqual((entry["distance_meters"], entry["duration_seconds"]), (6001, 61))

    def test_read_failure_write_uncertainty_auth_and_rate_limit_are_distinct(self):
        for failure, expected in (
            (("GET", "/exercise_templates", OSError("invented-key")), "not_sent"),
            (("GET", "/exercise_templates", 401), "needs_you"),
            (("POST", "/routines", OSError("invented-key")), "unknown"),
            (("POST", "/routines", 500), "unknown"),
            (("POST", "/routines", 400), "needs_you"),
            (("POST", "/routines", 429), "not_sent"),
            (("GET", "/routines/routine-1", 429), "unknown"),
        ):
            with self.subTest(failure=failure):
                self.http = FakeHevy()
                self.http.failure = failure
                result = self.send()
                self.assertEqual(result["status"], expected)
                self.assertNotIn("invented-key", json.dumps(result))
                self.assertLessEqual(len(self.http.writes()), 1)
        self.http = FakeHevy()
        self.http.failure = ("GET", "/exercise_templates", OSError("invented-key"))
        self.assertEqual(self.send(in_doubt=True)["status"], "unknown")

    def test_changed_readback_and_missing_acknowledgement_are_not_reported_sent(self):
        real = self.http

        def bad_reply(method, url, headers, body):
            status, response_headers, response = real(method, url, headers, body)
            if method == "POST":
                return status, response_headers, b"{}"
            return status, response_headers, response

        self.assertEqual(self.send(http=bad_reply)["status"], "unknown")
        self.assertEqual(len(real.writes()), 1)

    def test_full_free_account_without_a_named_routine_writes_nothing(self):
        # GIVEN no named routine and a full free account
        real = self.http

        def limited(method, url, headers, body):
            if method == "POST":
                return 403, {}, b'{"error":"routine-limit-exceeded","message":"invented-key"}'
            return real(method, url, headers, body)

        # WHEN TFS sends
        result = self.send(http=limited)
        # THEN nothing is written and the person is asked to pick an existing routine
        self.assertEqual(result["status"], "needs_you")
        self.assertEqual(real.saved, {})
        self.assertNotIn("auth_failed", result)
        self.assertIn("Pick an existing routine", result["message"])
        self.assertNotIn("invented-key", json.dumps(result))
