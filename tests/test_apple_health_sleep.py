import unittest
from pathlib import Path

from tests.helpers import read_records
from tfs.connectors.apple_health.connector import read

SAMPLE = Path(__file__).parent / "fixtures" / "apple_health" / "sample_export.xml"
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"


class AppleHealthSleepTest(unittest.TestCase):
    def test_stage_intervals_share_the_wake_date_and_keep_the_total(self) -> None:
        # GIVEN one export with all four stages, including core before midnight
        # WHEN the reader imports the night
        _, records = read_records(self, read, SAMPLE, "file")
        totals = {}
        for row in records:
            if row.get("metric", "").startswith("sleep_"):
                self.assertEqual(row["local_date"], "2026-03-02")
                metric = row["metric"]
                totals[metric] = totals.get(metric, 0) + row["values"]["value"]
        # THEN awake stays separate from the hours asleep
        self.assertEqual(
            totals,
            {
                "sleep_asleep": 6.6666667,
                "sleep_core": 0.6666667,
                "sleep_deep": 5.5,
                "sleep_rem": 0.5,
                "sleep_awake": 0.0833333,
            },
        )

    def setUp(self) -> None:
        _, records = read_records(self, read, SAMPLE, "file")
        self.sleep = [record for record in records if record.get("metric") == "sleep_asleep"]

    def _segment(self, start: str) -> dict:
        (found,) = (record for record in self.sleep if record["key"].split("|")[2] == start)
        return found

    def test_segment_that_ends_before_midnight_counts_toward_the_next_day(self) -> None:
        record = self._segment("2026-03-01 23:00:00 +1300")

        self.assertEqual(record["kind"], "measurement")
        self.assertEqual(record["stream"], "records")
        self.assertEqual(record["values"], {"value": 0.6666667, "unit": "h"})
        self.assertEqual(record["local_date"], "2026-03-02")
        self.assertEqual(record["start_utc"], "2026-03-01T10:00:00Z")
        self.assertEqual(record["end_utc"], "2026-03-01T10:40:00Z")
        self.assertEqual(record["utc_offset_min"], 780)
        self.assertEqual(record["origin_app"], "Ana's Apple Watch")
        self.assertEqual(record["origin_device"], "Apple Watch")
        self.assertEqual(
            record["key"],
            f"{SLEEP_TYPE}|Ana's Apple Watch|2026-03-01 23:00:00 +1300"
            "|2026-03-01 23:40:00 +1300|HKCategoryValueSleepAnalysisAsleepCore",
        )

    def test_overnight_segment_counts_toward_the_day_it_ends(self) -> None:
        record = self._segment("2026-03-02 01:00:00 +1300")

        self.assertEqual(record["values"], {"value": 5.5, "unit": "h"})
        self.assertEqual(record["local_date"], "2026-03-02")

    def test_afternoon_nap_counts_toward_the_same_day(self) -> None:
        record = self._segment("2026-03-02 14:00:00 +1300")

        self.assertEqual(record["values"], {"value": 0.5, "unit": "h"})
        self.assertEqual(record["local_date"], "2026-03-02")

    def test_in_bed_and_awake_give_no_record(self) -> None:
        values = [record["key"].rsplit("|", 1)[1] for record in self.sleep]

        self.assertEqual(len(self.sleep), 3)
        self.assertNotIn("HKCategoryValueSleepAnalysisInBed", values)
        self.assertNotIn("HKCategoryValueSleepAnalysisAwake", values)
