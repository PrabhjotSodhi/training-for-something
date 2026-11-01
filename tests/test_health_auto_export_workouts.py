import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from tfs.connectors.health_auto_export.connector import read
from tfs.connectors.testing.contract_checks import ConnectorContract

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "health_auto_export" / "workouts.json"
RUN_ID = "A1B2C3D4-0000-4000-8000-000000000001"


class HealthAutoExportWorkoutsTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        out_path = Path(folder.name) / "records.jsonl"
        self.result = read(str(FIXTURE_PATH), "rest_json", str(out_path))
        lines = out_path.read_text(encoding="utf-8").splitlines()
        self.sessions = {json.loads(line)["key"]: json.loads(line) for line in lines}

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


class HealthAutoExportWorkoutsContractTest(ConnectorContract, unittest.TestCase):
    module = "tfs.connectors.health_auto_export.connector"
    fixtures: ClassVar = [(str(FIXTURE_PATH), "rest_json")]
