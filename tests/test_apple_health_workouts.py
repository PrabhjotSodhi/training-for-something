import json
import tempfile
import unittest
from pathlib import Path

from tfs.connectors.apple_health.connector import read

SAMPLE = Path(__file__).parent / "fixtures" / "apple_health" / "sample_export.xml"
RUNNING = "HKWorkoutActivityTypeRunning"


class AppleHealthWorkoutsTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        out_path = Path(folder.name) / "records.jsonl"
        read(str(SAMPLE), "file", str(out_path))
        lines = out_path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        self.sessions = [record for record in records if record["kind"] == "session"]

    def _session(self, activity: str, start: str) -> dict:
        (found,) = (
            record
            for record in self.sessions
            if record["key"].startswith(f"HKWorkoutActivityType{activity}|")
            and record["key"].split("|")[2] == start
        )
        return found

    def test_running_workout_becomes_a_session_with_its_statistics(self) -> None:
        record = self._session("Running", "2026-03-02 06:00:00 +1300")

        self.assertEqual(
            record,
            {
                "kind": "session",
                "stream": "workouts",
                "key": f"{RUNNING}|Ana's Apple Watch|2026-03-02 06:00:00 +1300"
                "|2026-03-02 06:30:30 +1300",
                "revision_at": None,
                "start_utc": "2026-03-01T17:00:00Z",
                "end_utc": "2026-03-01T17:30:30Z",
                "utc_offset_min": 780,
                "local_date": "2026-03-02",
                "label": "Running",
                "family": "run",
                "values": {
                    "duration_s": 1830.0,
                    "distance_m": 5200.0,
                    "active_energy_kcal": 310.0,
                    "avg_hr": 148.0,
                    "max_hr": 171.0,
                },
                "origin_app": "Ana's Apple Watch",
                "origin_device": "Apple Watch",
                "plan_ref": None,
            },
        )

    def test_strength_workout_from_hevy_has_the_strength_family(self) -> None:
        record = self._session("TraditionalStrengthTraining", "2026-03-02 18:00:00 +1300")

        self.assertEqual(record["family"], "strength")
        self.assertEqual(record["origin_app"], "Hevy")
        self.assertEqual(record["values"], {"duration_s": 2700.0})

    def test_sport_with_no_family_keeps_its_label(self) -> None:
        record = self._session("Curling", "2026-03-02 20:00:00 +1300")

        self.assertIsNone(record["family"])
        self.assertEqual(record["label"], "Curling")

    def test_older_workout_reads_distance_and_energy_from_its_own_attributes(self) -> None:
        record = self._session("Walking", "2026-03-03 07:00:00 +1300")

        self.assertEqual(record["family"], "walk_hike")
        self.assertAlmostEqual(record["values"]["distance_m"], 4988.9664, places=7)
        self.assertEqual(record["values"]["active_energy_kcal"], 250.0)
        self.assertNotIn("avg_hr", record["values"])


class AppleHealthYardWorkoutTest(unittest.TestCase):
    def test_workout_measured_in_yards_keeps_its_distance_in_metres(self) -> None:
        export = (
            "<HealthData>"
            '<Workout workoutActivityType="HKWorkoutActivityTypeSwimming" duration="30"'
            ' durationUnit="min" totalDistance="1000" totalDistanceUnit="yd"'
            ' sourceName="Ana\'s Apple Watch" creationDate="2026-03-04 07:40:00 +1300"'
            ' startDate="2026-03-04 07:00:00 +1300" endDate="2026-03-04 07:30:00 +1300"/>'
            "</HealthData>"
        )
        with tempfile.TemporaryDirectory() as folder:
            export_path = Path(folder) / "export.xml"
            out_path = Path(folder) / "records.jsonl"
            export_path.write_text(export, encoding="utf-8")

            result = read(str(export_path), "file", str(out_path))

            (line,) = out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(result, {"status": "ok", "count": 1, "message": None})
        self.assertAlmostEqual(json.loads(line)["values"]["distance_m"], 914.4, places=7)
