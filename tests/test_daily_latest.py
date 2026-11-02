import json
import tempfile
import unittest
from pathlib import Path

from tfs.importer.session import rebuild_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.source_priority.daily_totals import rebuild

FILE_SHA256 = "0" * 64
DAY = "2026-10-01"


class DailyLatestTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = connect(Path(folder.name) / "tfs.db")
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
            " received_at) VALUES (?, 1, 'hevy', 'json', 'json', 1, '2026-10-01T00:00:00Z')",
            (FILE_SHA256,),
        )

    def add(
        self,
        key: str,
        metric: str,
        start_utc: str,
        value: float,
        *,
        connector: str = "apple_health",
        origin_app: str | None = None,
        origin_device: str | None = None,
        local_date: str = DAY,
        deleted_at: str | None = None,
    ) -> None:
        unit = {"weight": "kg", "resting_hr": "bpm"}[metric]
        self.conn.execute(
            "INSERT INTO source_records (id, connector, stream, key, kind, revision_at,"
            " arrival_seq, file_sha256, reader_version, start_utc, local_date, metric,"
            " values_json, origin_app, origin_device, deleted_at)"
            " VALUES (?, ?, ?, ?, 'measurement', '2026-10-01T00:00:00.000Z', 1, ?, 'r1', ?, ?, ?,"
            " ?, ?, ?, ?)",
            (
                key,
                connector,
                metric,
                key,
                FILE_SHA256,
                start_utc,
                local_date,
                metric,
                json.dumps({"value": value, "unit": unit}),
                origin_app,
                origin_device,
                deleted_at,
            ),
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
