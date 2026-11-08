import copy
import json
import unittest
from unittest import mock
from urllib.parse import urlsplit

from tfs.connectors.core.runner import run
from tfs.connectors.hevy import routines

ITEM = {
    "family": "strength",
    "status": "saved",
    "local_date": "2026-11-08",
    "label": "Push café ✓",
    "parts": [{"part_kind": "set", "label": "Press", "reps": 8, "weight_kg": 30.125}],
}
VERSION = "2026-11-08T00:00:00Z"


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
            value = {"routine": self.saved[path.rsplit("/", 1)[1]]}
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
        patch = mock.patch.object(routines, "_WRITES_PROVEN", True)
        patch.start()
        self.addCleanup(patch.stop)
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

    def test_default_gate_makes_no_request_even_in_real_helper(self):
        with mock.patch.object(routines, "_WRITES_PROVEN", False):
            self.assertEqual(self.send()["status"], "needs_you")
        self.assertEqual(self.http.calls, [])
        result = run(
            "hevy",
            "send",
            {
                "item": ITEM,
                "op_id": "operation-1",
                "remote_id": None,
                "remote_version": None,
                "in_doubt": False,
                "secrets": {"api_key": "invented-key"},
                "now": VERSION,
            },
            timeout_s=10,
        )
        self.assertEqual(result["status"], "needs_you")
        self.assertIn("verification", result["message"])

    def test_removed_routine_returns_manual_instruction_without_http(self):
        with mock.patch.object(routines, "_WRITES_PROVEN", False):
            result = self.send(item={**ITEM, "status": "removed"}, remote_id="routine-1")
        self.assertEqual(
            (result["status"], result["manual_action"], result["remote_id"]),
            ("needs_you", "delete", "routine-1"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertIn("cannot delete", result["message"])

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
        self.assertEqual(value["title"], "2026-11-08 Push café ✓ [TFS:operation-1]")
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
                self.assertEqual(self.send(item)["status"], "needs_you")
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

    def test_routine_limit_is_not_an_authentication_failure(self):
        real = self.http

        def limited(method, url, headers, body):
            if method == "POST":
                return 403, {}, b'{"error":"routine-limit-exceeded","message":"invented-key"}'
            return real(method, url, headers, body)

        result = self.send(http=limited)
        self.assertEqual(result["status"], "needs_you")
        self.assertNotIn("auth_failed", result)
        self.assertIn("routine limit", result["message"])
        self.assertNotIn("invented-key", json.dumps(result))
