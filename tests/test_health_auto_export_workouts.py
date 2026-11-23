import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import read_records
from tfs.connectors.health_auto_export.connector import read

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "health_auto_export" / "workouts.json"
RUN_ID = "A1B2C3D4-0000-4000-8000-000000000001"


class HealthAutoExportWorkoutsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result, records = read_records(self, read, FIXTURE_PATH, "rest_json")
        self.sessions = {record["key"]: record for record in records}

    def test_run_becomes_a_session_with_its_values(self) -> None:
        self.assertEqual(
            self.sessions[RUN_ID],
            {
                "kind": "session",
                "stream": "workouts",
                "key": RUN_ID,
                "revision_at": None,
                "start_utc": "2026-10-21T17:00:00Z",
                "end_utc": "2026-10-21T17:41:00Z",
                "utc_offset_min": 780,
                "local_date": "2026-10-22",
                "label": "Outdoor Run",
                "family": "run",
                "values": {
                    "duration_s": 2460.0,
                    "distance_m": 8020.0,
                    "active_energy_kcal": 512.3,
                    "avg_hr": 151.2,
                    "max_hr": 176.0,
                },
                "origin_app": None,
                "origin_device": None,
                "plan_ref": None,
            },
        )

    def test_strength_workout_reads_heart_rate_from_its_own_fields(self) -> None:
        record = self.sessions["A1B2C3D4-0000-4000-8000-000000000002"]

        self.assertEqual(record["family"], "strength")
        self.assertEqual(record["values"]["avg_hr"], 118.0)
        self.assertEqual(record["values"]["max_hr"], 141.0)

    def test_effort_rating_and_estimate_are_read_when_present(self) -> None:
        # GIVEN a rated workout in the fixture and one that only has an estimate
        estimated = Path(tempfile.mkdtemp()) / "estimated.json"
        self.addCleanup(shutil.rmtree, estimated.parent)
        estimated.write_text(
            json.dumps(
                {
                    "data": {
                        "workouts": [
                            {
                                "id": "e1",
                                "start": "2026-10-22 06:00:00 +1300",
                                "end": "2026-10-22 06:30:00 +1300",
                                "estimatedEffortScore": {"qty": 6, "units": "appleEffortScore"},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        # WHEN both are read
        _, records = read_records(self, read, estimated, "rest_json")

        # THEN the rating and the estimate land in their own values
        rated = self.sessions["A1B2C3D4-0000-4000-8000-000000000002"]["values"]
        self.assertEqual(rated["effort_rating"], 7.0)
        self.assertNotIn("effort_estimate", rated)
        self.assertEqual(records[0]["values"], {"effort_estimate": 6.0})

    def test_sport_with_no_family_keeps_its_label(self) -> None:
        record = self.sessions["A1B2C3D4-0000-4000-8000-000000000003"]

        self.assertIsNone(record["family"])
        self.assertEqual(record["label"], "Curling")
        self.assertEqual(record["values"], {})

    def test_workout_without_an_id_is_skipped_and_counted(self) -> None:
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(
            self.result,
            {"status": "partial", "count": 3, "message": "Skipped 1 workout without an id."},
        )
