import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar

from tests.helpers import read_records
from tfs.connectors.apple_health.connector import read
from tfs.connectors.testing.contract_checks import ConnectorContract

SAMPLE = Path(__file__).parent / "fixtures" / "apple_health" / "sample_export.xml"
FIXTURE = (str(SAMPLE), "file")

STEPS_KEY = (
    "HKQuantityTypeIdentifierStepCount|Ana's Apple Watch|2026-03-01 07:12:33 +1300"
    "|2026-03-01 07:20:00 +1300|532"
)


def _record(type_: str, unit: str, value: str) -> str:
    return (
        f'<Record type="{type_}" sourceName="Ana\'s iPhone" unit="{unit}" '
        'creationDate="2026-03-01 09:00:00 +1300" startDate="2026-03-01 09:00:00 +1300" '
        f'endDate="2026-03-01 09:00:00 +1300" value="{value}"/>\n'
    )


class AppleHealthReadTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def _read(self, path: Path) -> tuple[dict, list[dict]]:
        return read_records(self, read, path, "file")

    def _export_with(self, extra_records: str) -> Path:
        text = SAMPLE.read_text(encoding="utf-8").replace(
            "</HealthData>", extra_records + "</HealthData>"
        )
        path = self.folder / "export.xml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_step_record_becomes_a_measurement(self) -> None:
        result, records = self._read(SAMPLE)

        self.assertEqual(result, {"status": "ok", "count": 18, "message": None})
        self.assertEqual(
            records[0],
            {
                "kind": "measurement",
                "stream": "records",
                "key": STEPS_KEY,
                "revision_at": None,
                "start_utc": "2026-02-28T18:12:33Z",
                "end_utc": "2026-02-28T18:20:00Z",
                "utc_offset_min": 780,
                "local_date": "2026-03-01",
                "label": None,
                "family": None,
                "metric": "steps",
                "values": {"value": 532.0, "unit": "count"},
                "origin_app": "Ana's Apple Watch",
                "origin_device": "Apple Watch",
                "plan_ref": None,
            },
        )

    def test_units_convert_and_every_mapped_type_is_read(self) -> None:
        _, records = self._read(SAMPLE)

        by_metric = {
            record["metric"]: record for record in records if record["kind"] == "measurement"
        }
        self.assertEqual(
            list(by_metric),
            [
                "steps",
                "active_energy",
                "weight",
                "resting_hr",
                "heart_rate",
                "hrv_sdnn",
                "sleep_asleep",
                "sleep_core",
                "sleep_deep",
                "sleep_rem",
                "sleep_awake",
            ],
        )
        self.assertAlmostEqual(by_metric["active_energy"]["values"]["value"], 100.0, places=7)
        self.assertEqual(by_metric["active_energy"]["values"]["unit"], "kcal")
        self.assertAlmostEqual(by_metric["weight"]["values"]["value"], 69.85322498, places=7)
        self.assertEqual(by_metric["weight"]["values"]["unit"], "kg")
        self.assertIsNone(by_metric["weight"]["origin_device"])

    def test_body_composition_types_are_read_in_stored_units_with_their_source(self) -> None:
        path = self._export_with(
            _record("HKQuantityTypeIdentifierLeanBodyMass", "lb", "100")
            + _record("HKQuantityTypeIdentifierWaistCircumference", "in", "30")
            + _record("HKQuantityTypeIdentifierBodyMassIndex", "count", "22.5")
            + _record("HKQuantityTypeIdentifierHeight", "ft", "5")
        )

        _, records = self._read(path)

        found = {
            record["metric"]: record
            for record in records
            if record.get("metric") in ("lean_body_mass", "waist", "bmi", "height")
        }
        self.assertEqual(
            {
                name: (round(r["values"]["value"], 4), r["values"]["unit"])
                for name, r in found.items()
            },
            {
                "lean_body_mass": (45.3592, "kg"),
                "waist": (76.2, "cm"),
                "bmi": (22.5, "count"),
                "height": (152.4, "cm"),
            },
        )
        self.assertEqual({r["origin_app"] for r in found.values()}, {"Ana's iPhone"})

    def test_other_types_are_skipped_silently(self) -> None:
        path = self._export_with(_record("HKQuantityTypeIdentifierDietaryWater", "mL", "250"))

        result, records = self._read(path)

        self.assertEqual(result, {"status": "ok", "count": 18, "message": None})
        self.assertEqual(len(records), 18)

    def test_value_that_is_not_a_number_makes_the_file_partial(self) -> None:
        path = self._export_with(_record("HKQuantityTypeIdentifierStepCount", "count", "abc"))

        result, records = self._read(path)

        self.assertEqual(
            result,
            {
                "status": "partial",
                "count": 18,
                "message": "Skipped 1 record with a value that is not a number.",
            },
        )
        self.assertEqual(len(records), 18)

    def test_cut_off_export_is_unreadable_and_writes_nothing(self) -> None:
        text = SAMPLE.read_text(encoding="utf-8")
        path = self.folder / "export.xml"
        path.write_text(text[: text.index('endDate="2026-03-01 07:20:00') + 20], encoding="utf-8")

        result, records = self._read(path)

        self.assertEqual(result["status"], "unreadable")
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["message"].startswith("The export is incomplete or damaged"))
        self.assertEqual(records, [])

    def test_zip_gives_the_same_records_as_the_xml(self) -> None:
        archive = self.folder / "export.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.write(SAMPLE, "apple_health_export/export.xml")
            zipped.writestr("apple_health_export/export_cda.xml", "<ClinicalDocument>")

        _, from_xml = self._read(SAMPLE)
        result, from_zip = self._read(archive)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(from_zip, from_xml)

    def test_zip_without_an_export_is_unreadable(self) -> None:
        archive = self.folder / "export.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("notes.txt", "hello")

        result, records = self._read(archive)

        self.assertEqual(result["status"], "unreadable")
        self.assertEqual(records, [])


class AppleHealthContractTest(ConnectorContract, unittest.TestCase):
    module = "tfs.connectors.apple_health.connector"
    fixtures: ClassVar = [FIXTURE]
