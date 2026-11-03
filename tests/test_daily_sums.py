import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from tfs.importer.session import rebuild_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.source_priority.daily_totals import rebuild

FILE_SHA256 = "0" * 64
DAY = "2026-10-20"
NZ = timezone(timedelta(hours=13))
WATCH = "Watch"
PHONE = "Phone"


class DailySumsTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = connect(Path(folder.name) / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
            " received_at) VALUES (?, 1, 'hevy', 'json', 'json', 1, '2026-10-21T00:00:00Z')",
            (FILE_SHA256,),
        )
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
        unit = {"steps": "count", "sleep_asleep": "h"}[metric]
        values = {"value": value, "unit": unit, **({"merged": True} if merged else {})}
        self.conn.execute(
            "INSERT INTO source_records (id, connector, stream, key, kind, revision_at,"
            " arrival_seq, file_sha256, reader_version, start_utc, end_utc, local_date, metric,"
            " values_json, origin_app, origin_device)"
            " VALUES (?, ?, ?, ?, 'measurement', '2026-10-21T00:00:00.000Z', 1, ?, 'r1', ?, ?,"
            " ?, ?, ?, 'Health', ?)",
            (
                f"r{self.count}",
                connector,
                metric,
                f"r{self.count}",
                FILE_SHA256,
                utc(start),
                end and utc(end),
                day,
                metric,
                json.dumps(values),
                device,
            ),
        )

    def facts(self) -> list[tuple]:
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT local_date, metric, value, source, coverage, formula_version"
                " FROM day_facts ORDER BY local_date, metric"
            )
        ]

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
