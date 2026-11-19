import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from tests.helpers import read_records
from tfs.connectors.health_auto_export.connector import read
from tfs.connectors.testing.contract_checks import ConnectorContract

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "health_auto_export" / "metrics.json"
FIXTURE = (str(FIXTURE_PATH), "rest_json")
WORKOUTS_FIXTURE = (str(FIXTURE_PATH.with_name("workouts.json")), "rest_json")

STEPS_DATE = "2026-10-20 00:00:00 +1300"
STEPS_SOURCE = "Ana's iPhone|Ana's Apple Watch"


class HealthAutoExportReadTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def _read_body(self, body: str) -> tuple[dict, list[dict]]:
        path = self.folder / "payload.json"
        path.write_text(body, encoding="utf-8")
        return read_records(self, read, path, "rest_json")

    def _read_fixture(self) -> tuple[dict, dict[str, dict]]:
        result, records = self._read_body(FIXTURE_PATH.read_text(encoding="utf-8"))
        return result, {record["metric"] + "|" + record["key"]: record for record in records}

    def test_merged_step_total_becomes_a_measurement(self) -> None:
        result, records = self._read_fixture()

        self.assertEqual(result, {"status": "ok", "count": 8, "message": None})
        self.assertEqual(
            records[f"steps|step_count|{STEPS_SOURCE}|{STEPS_DATE}"],
            {
                "kind": "measurement",
                "stream": "metrics",
                "key": f"step_count|{STEPS_SOURCE}|{STEPS_DATE}",
                "revision_at": None,
                "start_utc": "2026-10-19T11:00:00Z",
                "end_utc": None,
                "utc_offset_min": 780,
                "local_date": "2026-10-20",
                "label": None,
                "family": None,
                "metric": "steps",
                "values": {"value": 4210.0, "unit": "count", "merged": True},
                "origin_app": "Health Auto Export",
                "origin_device": STEPS_SOURCE,
                "plan_ref": None,
            },
        )

    def test_kilojoules_become_kcal(self) -> None:
        _, records = self._read_fixture()

        record = records[f"active_energy|active_energy|Ana's Apple Watch|{STEPS_DATE}"]
        self.assertEqual(record["values"], {"value": 100.0, "unit": "kcal"})

    def test_heart_rate_keeps_average_min_and_max(self) -> None:
        _, records = self._read_fixture()

        record = records["heart_rate|heart_rate|Ana's Apple Watch|2026-10-20 09:00:00 +1300"]
        self.assertEqual(record["values"], {"value": 72.5, "min": 61.0, "max": 98.0, "unit": "bpm"})

    def test_sample_without_source_and_unmapped_metric(self) -> None:
        result, records = self._read_fixture()

        record = records["steps|step_count|-|2026-10-21 00:00:00 +1300"]
        self.assertIsNone(record["origin_device"])
        self.assertEqual(record["values"], {"value": 3050.0, "unit": "count"})
        self.assertFalse([key for key in records if "dietary_water" in key])
        self.assertEqual(result["status"], "ok")

    def test_vo2_max_keeps_its_value_and_unit(self) -> None:
        # GIVEN a push with one VO2 max sample
        body = json.dumps(
            {
                "data": {
                    "metrics": [
                        {
                            "name": "vo2_max",
                            "units": "ml/(kg·min)",
                            "data": [{"date": "2026-03-02 08:00:00 +1300", "qty": 41.3}],
                        },
                        {
                            "name": "body_fat_percentage",
                            "units": "%",
                            "data": [{"date": "2026-03-02 08:00:00 +1300", "qty": 21}],
                        },
                    ]
                }
            }
        )

        # WHEN it is read
        _, records = self._read_body(body)

        # THEN both become measurements in TFS units
        self.assertEqual(
            [(record["metric"], record["values"], record["local_date"]) for record in records],
            [
                ("vo2_max", {"value": 41.3, "unit": "mL/kg/min"}, "2026-03-02"),
                ("body_fat", {"value": 21.0, "unit": "%"}, "2026-03-02"),
            ],
        )

    def test_other_json_is_unreadable(self) -> None:
        result, records = self._read_body('{"foo": 1}')

        self.assertEqual(
            result,
            {"status": "unreadable", "count": 0, "message": "Not a Health Auto Export payload."},
        )
        self.assertEqual(records, [])

    def test_bad_samples_are_skipped_and_counted(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "metrics": [
                        {
                            "name": "step_count",
                            "units": "count",
                            "data": [
                                {"date": STEPS_DATE, "qty": "many"},
                                {"date": "yesterday", "qty": 10},
                                {"date": STEPS_DATE, "qty": 7, "source": "Ana's iPhone"},
                            ],
                        }
                    ]
                }
            }
        )

        result, records = self._read_body(body)

        self.assertEqual(
            result,
            {
                "status": "partial",
                "count": 1,
                "message": "Skipped 1 sample with a value that is not a number."
                " Skipped 1 sample with a time that TFS cannot read.",
            },
        )
        self.assertEqual(records[0]["values"]["value"], 7.0)


class HealthAutoExportContractTest(ConnectorContract, unittest.TestCase):
    module = "tfs.connectors.health_auto_export.connector"
    fixtures: ClassVar = [FIXTURE, WORKOUTS_FIXTURE]
