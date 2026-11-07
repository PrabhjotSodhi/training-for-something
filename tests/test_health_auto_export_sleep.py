import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import read_records
from tfs.connectors.health_auto_export.connector import read

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "health_auto_export" / "metrics.json"
SLEEP_KEY = "sleep_analysis|Ana's Apple Watch|2026-10-20 22:40:00 +1300"


class HealthAutoExportSleepTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def _read_body(self, body: str) -> tuple[dict, list[dict]]:
        path = self.folder / "payload.json"
        path.write_text(body, encoding="utf-8")
        return read_records(self, read, path, "rest_json")

    def _read_sleep(self, sample: dict) -> tuple[dict, list[dict]]:
        body = {"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [sample]}]}}
        return self._read_body(json.dumps(body))

    def test_night_counts_toward_the_day_you_wake_up(self) -> None:
        _, records = self._read_body(FIXTURE_PATH.read_text(encoding="utf-8"))

        (record,) = (found for found in records if found["metric"] == "sleep_asleep")
        self.assertEqual(
            record,
            {
                "kind": "measurement",
                "stream": "metrics",
                "key": SLEEP_KEY,
                "revision_at": None,
                "start_utc": "2026-10-20T09:40:00Z",
                "end_utc": "2026-10-20T17:10:00Z",
                "utc_offset_min": 780,
                "local_date": "2026-10-21",
                "label": None,
                "family": None,
                "metric": "sleep_asleep",
                "values": {"value": 7.05, "unit": "h"},
                "origin_app": "Health Auto Export",
                "origin_device": "Ana's Apple Watch",
                "plan_ref": None,
            },
        )

    def test_date_without_a_time_gives_the_same_record(self) -> None:
        sample = {
            "date": "2026-10-21",
            "sleepStart": "2026-10-20 22:40:00 +1300",
            "sleepEnd": "2026-10-21 06:10:00 +1300",
            "totalSleep": 7.05,
            "source": "Ana's Apple Watch",
        }

        _, records = self._read_sleep(sample)

        (record,) = records
        self.assertEqual(record["key"], SLEEP_KEY)
        self.assertEqual(record["start_utc"], "2026-10-20T09:40:00Z")
        self.assertEqual(record["local_date"], "2026-10-21")

    def test_asleep_is_used_when_total_sleep_is_missing(self) -> None:
        sample = {
            "date": "2026-10-21 00:00:00 +1300",
            "sleepStart": "2026-10-20 22:40:00 +1300",
            "sleepEnd": "2026-10-21 06:10:00 +1300",
            "asleep": 6.5,
            "source": "Ana's Apple Watch",
        }

        _, records = self._read_sleep(sample)

        (record,) = records
        self.assertEqual(record["values"], {"value": 6.5, "unit": "h"})

    def test_sample_without_sleep_times_is_skipped_with_a_hint(self) -> None:
        sample = {"date": "2026-10-21 00:00:00 +1300", "asleep": 6.5, "source": "Ana's iPhone"}

        result, records = self._read_sleep(sample)

        self.assertEqual(
            result,
            {
                "status": "partial",
                "count": 0,
                "message": "Turn on Aggregate Sleep Data in Health Auto Export.",
            },
        )
        self.assertEqual(records, [])
