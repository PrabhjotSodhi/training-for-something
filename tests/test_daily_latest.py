import unittest

from tests.helpers import insert_file, insert_measurement, open_database
from tfs.connectors.core.contract import METRICS
from tfs.importer.session import rebuild_pending
from tfs.source_priority.daily_totals import rebuild

DAY = "2026-10-01"


class DailyLatestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        insert_file(self.conn, 1, "2026-10-01T00:00:00Z")

    def add(
        self,
        key: str,
        metric: str,
        start_utc: str,
        value: float,
        *,
        connector: str = "apple_health",
        local_date: str = DAY,
        **fields,
    ) -> None:
        values = {"value": value, "unit": METRICS[metric].unit}
        insert_measurement(
            self.conn, key, connector, metric, start_utc, values, local_date=local_date, **fields
        )

    def facts(self) -> list[tuple]:
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT local_date, metric, value, unit, source, coverage, formula_version"
                " FROM day_facts ORDER BY local_date, metric"
            )
        ]

    def test_the_latest_weight_of_the_day_wins_across_sources(self) -> None:
        self.add("a", "weight", "2026-10-01T07:00:00Z", 70.1, origin_app="Scale App")
        self.add(
            "b",
            "weight",
            "2026-10-01T21:00:00Z",
            70.6,
            connector="health_auto_export",
            origin_app="Health Auto Export",
            origin_device="Bathroom Scale",
        )

        written = rebuild(self.conn, {DAY})

        self.assertEqual(written, 1)
        self.assertEqual(
            self.facts(),
            [
                (
                    DAY,
                    "weight",
                    70.6,
                    "kg",
                    "health_auto_export/weight/Health Auto Export/Bathroom Scale",
                    "full",
                    "day-facts-1",
                )
            ],
        )

    def test_a_resting_heart_rate_of_zero_is_missing(self) -> None:
        self.add("a", "resting_hr", "2026-10-01T06:00:00Z", 0)
        self.add("b", "resting_hr", "2026-10-01T05:00:00Z", 52)

        rebuild(self.conn, {DAY})

        self.assertEqual([(fact[1], fact[2]) for fact in self.facts()], [("resting_hr", 52)])

    def test_a_day_with_no_readings_has_no_row_and_a_day_that_lost_them_loses_its_row(self) -> None:
        self.add("a", "weight", "2026-10-01T07:00:00Z", 70.1)
        rebuild(self.conn, {DAY, "2026-10-02"})
        self.assertEqual([fact[0] for fact in self.facts()], [DAY])

        self.conn.execute("UPDATE source_records SET deleted_at = '2026-10-03T00:00:00Z'")
        rebuild(self.conn, {DAY, "2026-10-02"})

        self.assertEqual(self.facts(), [])

    def test_readings_at_the_same_instant_resolve_to_the_smaller_source_whatever_the_order(
        self,
    ) -> None:
        readings = [
            ("a", "weight", "2026-10-01T07:00:00Z", 70.1, "Zeta Scale"),
            ("b", "weight", "2026-10-01T07:00:00Z", 70.3, "Alpha Scale"),
        ]
        for ordered in (readings, readings[::-1]):
            self.conn.execute("DELETE FROM source_records")
            for key, metric, start_utc, value, device in ordered:
                self.add(key, metric, start_utc, value, origin_device=device)

            rebuild(self.conn, {DAY})

            self.assertEqual(
                [(fact[2], fact[4]) for fact in self.facts()],
                [(70.3, "apple_health/weight/-/Alpha Scale")],
            )

    def test_each_body_composition_metric_gets_one_day_value_from_two_routes(self) -> None:
        for number, metric in enumerate(("lean_body_mass", "waist", "bmi", "height")):
            self.add(f"a{number}", metric, "2026-10-01T07:00:00Z", 60.0, origin_app="Apple Health")
            self.add(
                f"b{number}",
                metric,
                "2026-10-01T08:00:00Z",
                61.0,
                connector="health_auto_export",
                origin_app="Health Auto Export",
            )

        rebuild(self.conn, {DAY})

        self.assertEqual(
            [(fact[1], fact[2], fact[3]) for fact in self.facts()],
            [
                ("bmi", 61.0, "count"),
                ("height", 61.0, "cm"),
                ("lean_body_mass", 61.0, "kg"),
                ("waist", 61.0, "cm"),
            ],
        )

    def test_other_dates_keep_their_rows(self) -> None:
        self.add("a", "weight", "2026-10-01T07:00:00Z", 70.1)
        self.add("b", "weight", "2026-10-02T07:00:00Z", 70.2, local_date="2026-10-02")
        rebuild(self.conn, {DAY, "2026-10-02"})
        self.conn.execute("UPDATE source_records SET deleted_at = '2026-10-03T00:00:00Z'")

        rebuild(self.conn, {DAY})

        self.assertEqual([(fact[0], fact[2]) for fact in self.facts()], [("2026-10-02", 70.2)])

    def test_rebuilding_pending_dates_writes_the_day_facts(self) -> None:
        self.add("a", "resting_hr", "2026-10-01T06:00:00Z", 55)
        self.conn.execute("INSERT INTO rebuild_dates (local_date) VALUES (?)", (DAY,))

        rebuild_pending(self.conn)

        self.assertEqual([(fact[1], fact[2]) for fact in self.facts()], [("resting_hr", 55)])

    def test_hrv_is_the_mean_of_the_days_readings_and_zero_is_missing(self) -> None:
        # GIVEN readings of 40, 50 and 60 on one day, and 0 and 48 on another
        for key, start_utc, value in [
            ("a", "2026-10-01T01:00:00Z", 40),
            ("b", "2026-10-01T02:00:00Z", 50),
            ("c", "2026-10-01T03:00:00Z", 60),
        ]:
            self.add(key, "hrv_sdnn", start_utc, value)
        self.add("d", "hrv_sdnn", "2026-10-02T01:00:00Z", 0, local_date="2026-10-02")
        self.add("e", "hrv_sdnn", "2026-10-02T02:00:00Z", 48, local_date="2026-10-02")
        self.add("f", "hrv_sdnn", "2026-10-03T01:00:00Z", 0, local_date="2026-10-03")

        # WHEN the days are rebuilt
        rebuild(self.conn, {DAY, "2026-10-02", "2026-10-03"})

        # THEN the first day is the mean, the second ignores the 0 and the third has no row
        self.assertEqual(
            [(fact[0], fact[2]) for fact in self.facts()], [(DAY, 50), ("2026-10-02", 48)]
        )

    def test_hrv_of_one_day_comes_from_one_connector(self) -> None:
        # GIVEN two connectors with readings on one day, one with more readings
        self.add("a", "hrv_sdnn", "2026-10-01T01:00:00Z", 40)
        self.add("b", "hrv_sdnn", "2026-10-01T02:00:00Z", 60)
        self.add("c", "hrv_sdnn", "2026-10-01T03:00:00Z", 100, connector="health_auto_export")

        # WHEN the day is rebuilt
        rebuild(self.conn, {DAY})

        # THEN only the connector with more readings counts
        self.assertEqual(
            [(fact[2], fact[4]) for fact in self.facts()], [(50, "apple_health/hrv_sdnn/-/-")]
        )
