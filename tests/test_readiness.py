import json
import unittest
from datetime import date, timedelta

from tests.helpers import open_database
from tfs.importer.session import rebuild_pending
from tfs.scores import readiness

START = date(2026, 6, 1)


class ReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)

    def add(self, metric: str, first_day: int, count: int, *, source: str = "watch A") -> None:
        """Add one fact a day, alternating 41 and 43 so the normal is 42 with some spread."""
        for offset in range(first_day, first_day + count):
            self.fact(metric, offset, 41 + 2 * (offset % 2), source)

    def fact(self, metric: str, offset: int, value: float, source: str = "watch A") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO day_facts VALUES (?, ?, ?, 'x', ?, 'full', 'day-facts-1')",
            ((START + timedelta(days=offset)).isoformat(), metric, value, source),
        )

    def history(self, metrics=("hrv_sdnn", "resting_hr", "sleep_asleep"), days: int = 42) -> None:
        for metric in metrics:
            self.add(metric, 0, days)

    def today(self, offset: int = 42) -> tuple:
        readiness.rebuild(self.conn)
        row = self.conn.execute(
            "SELECT value, low, high, inputs_json FROM scores WHERE local_date = ?",
            ((START + timedelta(days=offset)).isoformat(),),
        ).fetchone()
        return row[0], row[1], row[2], json.loads(row[3])

    def test_values_at_their_medians_give_50_with_a_range(self) -> None:
        self.history()
        for metric in ("hrv_sdnn", "resting_hr", "sleep_asleep"):
            self.fact(metric, 42, 42)

        value, low, high, inputs = self.today()

        self.assertEqual(value, 50)
        self.assertLess(low, 50)
        self.assertGreater(high, 50)
        self.assertEqual(inputs["hrv_sdnn"]["baseline_days"], 42)

    def test_higher_hrv_raises_readiness_and_higher_resting_hr_lowers_it(self) -> None:
        self.history()
        for metric in ("resting_hr", "sleep_asleep"):
            self.fact(metric, 42, 42)
        self.fact("hrv_sdnn", 42, 45)
        self.assertGreater(self.today()[0], 50)

        self.fact("hrv_sdnn", 42, 42)
        self.fact("resting_hr", 42, 45)
        self.assertLess(self.today()[0], 50)

    def test_two_inputs_give_a_value_with_a_wider_range(self) -> None:
        self.history()
        for metric in ("hrv_sdnn", "resting_hr", "sleep_asleep"):
            self.fact(metric, 42, 42)
        _, three_low, three_high, _ = self.today()
        self.conn.execute("DELETE FROM day_facts WHERE metric = 'sleep_asleep' AND value = 42")

        value, low, high, inputs = self.today()

        self.assertEqual(value, 50)
        self.assertGreater(three_low - low, 0)
        self.assertGreater(high - three_high, 0)
        self.assertEqual(inputs["sleep_asleep"]["reason"], "no value today")

    def test_20_valid_days_give_no_value(self) -> None:
        self.history(days=20)
        for metric in ("hrv_sdnn", "resting_hr", "sleep_asleep"):
            self.fact(metric, 20, 42)

        value, _, _, inputs = self.today(20)

        self.assertIsNone(value)
        self.assertEqual(inputs["hrv_sdnn"]["reason"], "needs 21 days, has 20")

    def test_only_hrv_gives_no_value_and_names_the_missing_inputs(self) -> None:
        self.history(("hrv_sdnn",))
        self.fact("hrv_sdnn", 42, 42)

        value, _, _, inputs = self.today()

        self.assertIsNone(value)
        self.assertIn("resting_hr", inputs)
        self.assertIn("sleep_asleep", inputs)

    def test_a_new_device_starts_a_new_baseline(self) -> None:
        for metric in ("hrv_sdnn", "resting_hr", "sleep_asleep"):
            self.add(metric, 0, 40)
            self.add(metric, 40, 10, source="watch B")

        value, _, _, inputs = self.today(49)

        self.assertIsNone(value)
        self.assertEqual(inputs["hrv_sdnn"]["reason"], "needs 21 days, has 9")

    def test_rows_with_an_older_formula_version_are_rebuilt(self) -> None:
        self.history()
        readiness.rebuild(self.conn)
        self.conn.execute("UPDATE scores SET formula_version = 'old'")

        readiness.rebuild_if_outdated(self.conn)

        versions = {row[0] for row in self.conn.execute("SELECT formula_version FROM scores")}
        self.assertEqual(versions, {readiness.FORMULA_VERSION})

    def test_importing_rebuilds_the_scores(self) -> None:
        self.history()
        self.conn.execute("INSERT INTO rebuild_dates VALUES (?)", (START.isoformat(),))

        rebuild_pending(self.conn)

        self.assertGreater(self.conn.execute("SELECT count(*) FROM scores").fetchone()[0], 0)
