import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from tests.helpers import read_records
from tfs.connectors.hevy.connector import read
from tfs.connectors.testing.contract_checks import ConnectorContract

FIXTURES = Path(__file__).parent / "fixtures" / "hevy"
WORKOUTS_PAGE = (str(FIXTURES / "workouts_page.json"), "workouts_page")
EVENTS_PAGE = (str(FIXTURES / "events_page.json"), "events_page")
EVENTS_UNKNOWN = (str(FIXTURES / "events_unknown.json"), "events_page")


class HevyReadTest(unittest.TestCase):
    def _read(self, path: str, fmt: str) -> tuple[dict, list[dict]]:
        return read_records(self, read, path, fmt)

    def test_workout_becomes_session_with_a_part_per_set(self) -> None:
        result, records = self._read(*WORKOUTS_PAGE)

        self.assertEqual(result, {"status": "ok", "count": 1, "message": None})
        self.assertEqual(len(records), 1)
        session = records[0]
        parts = session.pop("parts")
        self.assertEqual(
            session,
            {
                "kind": "session",
                "stream": "workouts",
                "key": "w-001",
                "revision_at": "2026-10-20T19:06:12.000Z",
                "start_utc": "2026-10-20T18:00:00Z",
                "end_utc": "2026-10-20T19:05:00Z",
                "utc_offset_min": None,
                "local_date": None,
                "label": "Push A",
                "family": "strength",
                "values": {"duration_s": 3900.0},
                "origin_app": "com.hevyapp.hevy",
                "origin_device": None,
                "plan_ref": "r-010",
            },
        )
        self.assertEqual([part["seq"] for part in parts], [1, 2, 3, 4, 5])
        self.assertEqual(
            parts[3],
            {
                "seq": 4,
                "part_kind": "set",
                "label": "Plank",
                "reps": None,
                "weight_kg": None,
                "duration_s": 60,
                "distance_m": None,
                "extras": {
                    "set_type": "normal",
                    "rpe": None,
                    "exercise_index": 1,
                    "set_index": 0,
                    "exercise_template_id": "A7D5C2E1",
                },
            },
        )
        self.assertEqual(parts[0]["reps"], 10)
        self.assertEqual(parts[1]["weight_kg"], 60)
        self.assertEqual(parts[1]["extras"]["rpe"], 8)

    def test_events_page_gives_a_session_and_a_delete(self) -> None:
        result, records = self._read(*EVENTS_PAGE)

        self.assertEqual(result, {"status": "ok", "count": 2, "message": None})
        self.assertEqual(records[0]["key"], "w-003")
        self.assertIsNone(records[0]["plan_ref"])
        self.assertEqual(
            records[1],
            {
                "kind": "deleted",
                "stream": "workouts",
                "key": "w-002",
                "revision_at": "2026-10-21T08:00:00.000Z",
            },
        )

    def test_unknown_event_type_is_unreadable_and_writes_nothing(self) -> None:
        result, records = self._read(*EVENTS_UNKNOWN)

        self.assertEqual(result["status"], "unreadable")
        self.assertEqual(result["count"], 0)
        self.assertIn("archived", result["message"])
        self.assertEqual(records, [])

    def test_bad_json_is_unreadable_and_writes_nothing(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "broken.json"
        path.write_text('{"workouts": [', encoding="utf-8")

        result, records = self._read(str(path), "workouts_page")

        self.assertEqual(result["status"], "unreadable")
        self.assertEqual(result["count"], 0)
        self.assertEqual(records, [])


class HevyContractTest(ConnectorContract, unittest.TestCase):
    module = "tfs.connectors.hevy.connector"
    fixtures: ClassVar = [WORKOUTS_PAGE, EVENTS_PAGE]
    delete_fixture = EVENTS_PAGE
    fetch_args: ClassVar = {"cursor": None, "secrets": {"api_key": "invented-key-123"}}
