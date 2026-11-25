import unittest
from datetime import UTC, datetime, timedelta, timezone

from tests.helpers import insert_file, insert_measurement, open_database
from tfs.importer.session import rebuild_pending
from tfs.source_priority.daily_totals import rebuild

DAY = "2026-10-20"
NZ = timezone(timedelta(hours=13))
WATCH = "Watch"
PHONE = "Phone"


class DailySumsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        insert_file(self.conn, 1, "2026-10-21T00:00:00Z")
        self.count = 0

    def received(self, connector: str, last_received_at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (connector, last_received_at) VALUES (?, ?)",
            (connector, last_received_at),
        )

    def add(
        self,
        connector: str,
        device: str,
        start: str,
        end: str | None,
        value: float,
        *,
        metric: str = "steps",
        merged: bool = False,
        day: str = DAY,
    ) -> None:
        def utc(clock: str) -> str:
            local = datetime.fromisoformat(f"{day}T{clock}:00+13:00")
            return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.count += 1
        unit = "h" if metric.startswith("sleep_") else "count"
        values = {"value": value, "unit": unit, **({"merged": True} if merged else {})}
        insert_measurement(
            self.conn,
            f"r{self.count}",
            connector,
            metric,
            utc(start),
            values,
            local_date=day,
            end_utc=end and utc(end),
            origin_app="Health",
            origin_device=device,
            stream="sleep" if metric.startswith("sleep_") else metric,
        )

    def facts(self) -> list[tuple]:
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT local_date, metric, value, source, coverage, formula_version"
                " FROM day_facts ORDER BY local_date, metric"
            )
        ]

    def test_sleep_stages_follow_the_device_chosen_for_the_night(self) -> None:
        # GIVEN two devices with different stage totals
        for device, values in ((WATCH, (8, 2, 4, 2, 0)), (PHONE, (7, 3, 3, 1, 1))):
            for metric, value in zip(
                ("asleep", "deep", "core", "rem", "awake"), values, strict=True
            ):
                self.add("app", device, "00:00", "08:00", value, metric=f"sleep_{metric}")
        # WHEN totals build, every stage uses the same source as hours asleep
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(
            {metric: value for _, metric, value, *_ in self.facts()},
            {"sleep_asleep": 8, "sleep_deep": 2, "sleep_core": 4, "sleep_rem": 2, "sleep_awake": 0},
        )
        self.assertEqual({row[3] for row in self.facts()}, {"app/sleep/Health/Watch"})
        # THEN a missing stage on the winning device stays empty on rebuild
        self.conn.execute(
            "DELETE FROM source_records WHERE origin_device='Watch' AND metric='sleep_rem'"
        )
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertNotIn("sleep_rem", [row[1] for row in self.facts()])

    def test_a_source_marked_merged_wins_over_records_that_merge_to_more(self) -> None:
        self.received("health_auto_export", "2026-10-21T00:00:00Z")
        self.received("apple_health", "2026-10-21T00:00:00Z")
        self.add("health_auto_export", "Phone", "00:00", "23:59", 7400, merged=True)
        self.add("apple_health", WATCH, "08:00", "09:00", 6000)
        self.add("apple_health", PHONE, "10:00", "11:00", 3500)
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(
            self.facts(),
            [
                (
                    DAY,
                    "steps",
                    7400.0,
                    "health_auto_export/steps/Health/Phone",
                    "full",
                    "day-facts-1",
                )
            ],
        )

    def test_watch_and_phone_records_count_each_moment_once(self) -> None:
        self.received("apple_health", "2026-10-21T00:00:00Z")
        for start, end in (("08:00", "09:00"), ("12:00", "13:00"), ("18:00", "23:30")):
            self.add("apple_health", WATCH, start, end, 2000)
        self.add("apple_health", PHONE, "08:00", "09:00", 1000)
        self.add("apple_health", PHONE, "23:30", "23:50", 3500)
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(
            self.facts(),
            [
                (
                    DAY,
                    "steps",
                    9500.0,
                    "apple_health/steps/Health/Watch + apple_health/steps/Health/Phone",
                    "full",
                    "day-facts-1",
                )
            ],
        )

    def test_equal_totals_from_two_connectors_are_never_added(self) -> None:
        self.received("apple_health", "2026-10-21T00:00:00Z")
        self.received("health_auto_export", "2026-10-21T00:00:00Z")
        self.add("health_auto_export", WATCH, "00:00", None, 4210)
        self.add("apple_health", WATCH, "08:00", "09:00", 2000)
        self.add("apple_health", WATCH, "10:00", "11:00", 2210)
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(
            self.facts(),
            [(DAY, "steps", 4210.0, "apple_health/steps/Health/Watch", "full", "day-facts-1")],
        )

    def test_a_record_inside_a_longer_one_adds_only_its_uncovered_share(self) -> None:
        self.received("apple_health", "2026-10-21T00:00:00Z")
        self.add("apple_health", WATCH, "11:00", "12:00", 1500)
        self.add("apple_health", PHONE, "10:00", "12:00", 1000)
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(
            self.facts(),
            [
                (
                    DAY,
                    "steps",
                    2000.0,
                    "apple_health/steps/Health/Watch + apple_health/steps/Health/Phone",
                    "full",
                    "day-facts-1",
                )
            ],
        )

    def test_a_day_is_partial_until_data_arrives_past_its_end(self) -> None:
        self.received("apple_health", "2026-10-20T03:00:00Z")
        self.add("apple_health", WATCH, "08:00", "09:00", 100, day="2026-10-19")
        self.add("apple_health", WATCH, "08:00", "09:00", 200)
        self.add("apple_health", WATCH, "08:00", "09:00", 0.0, metric="sleep_asleep")
        rebuild(self.conn, {"2026-10-19", DAY}, tz=NZ)
        self.assertEqual(
            [(date, coverage) for date, _, _, _, coverage, _ in self.facts()],
            [("2026-10-19", "full"), (DAY, "partial")],
        )

    def test_a_partial_day_turns_full_when_pending_rebuild_runs_after_new_data(self) -> None:
        self.received("apple_health", "2026-10-20T03:00:00Z")
        self.add("apple_health", WATCH, "08:00", "09:00", 200)
        rebuild(self.conn, {DAY}, tz=NZ)
        self.assertEqual(self.facts()[0][4], "partial")
        self.received("apple_health", "2026-10-20T11:00:00Z")
        rebuild_pending(self.conn, tz=NZ)
        self.assertEqual(self.facts()[0][4], "full")


if __name__ == "__main__":
    unittest.main()
