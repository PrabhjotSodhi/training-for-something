import json
import unittest
from datetime import UTC, datetime, timedelta, tzinfo

from tests.helpers import open_database, store_file

RECEIVED_AT = {
    1: "2026-10-01T08:00:00Z",
    2: "2026-10-01T20:00:00Z",
    3: "2026-10-02T11:00:00.000Z",
}

STEPS = {
    "kind": "measurement",
    "stream": "records",
    "key": "k1",
    "revision_at": None,
    "start_utc": "2026-10-01T07:00:00Z",
    "end_utc": "2026-10-01T07:30:00Z",
    "utc_offset_min": None,
    "local_date": "2026-10-01",
    "metric": "steps",
    "values": {"value": 2000, "unit": "count"},
}

SESSION = {
    "kind": "session",
    "stream": "workouts",
    "key": "w1",
    "revision_at": None,
    "start_utc": "2026-10-02T09:00:00Z",
    "local_date": "2026-10-02",
    "label": "Push A",
    "family": "strength",
    "values": {"duration_s": 3600},
    "parts": [
        {
            "seq": 1,
            "part_kind": "set",
            "reps": 6,
            "weight_kg": 80.0,
            "extras": {"set_type": "normal"},
        },
        {"seq": 2, "part_kind": "set", "reps": 5, "weight_kg": 85.0},
    ],
}


class Auckland(tzinfo):
    """UTC+13 until 2026-04-04T14:00:00Z, UTC+12 after it."""

    switch = datetime(2026, 4, 4, 14, tzinfo=UTC)

    def utcoffset(self, dt):
        wall = dt.replace(tzinfo=UTC)
        return timedelta(hours=13 if wall < self.switch + timedelta(hours=13) else 12)

    def dst(self, dt):
        return timedelta(0)

    def fromutc(self, dt):
        offset = timedelta(hours=13 if dt.replace(tzinfo=UTC) < self.switch else 12)
        return dt + offset


def changed(rec, **fields):
    """Return a copy of rec with some fields replaced."""
    return {**rec, **fields}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(self, "import")

    def store(self, records, arrival=1, conn=None, reader_version="r1", **options):
        return store_file(
            conn or self.conn, records, arrival, RECEIVED_AT[arrival], reader_version, **options
        )

    def rows(self, conn=None):
        return (conn or self.conn).execute("SELECT * FROM source_records ORDER BY key").fetchall()

    def value_of(self, key="k1"):
        row = self.conn.execute("SELECT values_json FROM source_records WHERE key = ?", (key,))
        return json.loads(row.fetchone()[0])["value"]

    def test_storing_the_same_record_twice_adds_nothing(self):
        self.store([STEPS])
        second = self.store([STEPS])

        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((second.inserted, second.replaced, second.unchanged), (0, 0, 1))

    def test_a_later_arrival_replaces_and_an_earlier_one_does_not_come_back(self):
        self.store([STEPS], arrival=1)
        newer = self.store([changed(STEPS, values={"value": 9000, "unit": "count"})], arrival=2)
        self.assertEqual((newer.inserted, newer.replaced, newer.unchanged), (0, 1, 0))
        self.assertEqual(self.value_of(), 9000)

        older = self.store([STEPS], arrival=1)

        self.assertEqual((older.replaced, older.unchanged), (0, 1))
        self.assertEqual(self.value_of(), 9000)

    def test_the_higher_revision_wins_in_any_order(self):
        versions = [
            (changed(SESSION, label="A", revision_at="2026-10-02T10:00:00.900Z"), 1),
            (changed(SESSION, label="B", revision_at="2026-10-02T10:00:00.100Z"), 2),
            (changed(SESSION, label="C"), 3),
        ]
        for order in (versions, versions[::-1]):
            conn = open_database(self, "import")
            for rec, arrival in order:
                self.store([rec], arrival=arrival, conn=conn)
            (row,) = self.rows(conn)
            with self.subTest(first=order[0][0]["label"]):
                self.assertEqual(
                    (row["label"], row["revision_at"], row["arrival_seq"]),
                    ("C", "2026-10-02T11:00:00.000Z", 3),
                )

        conn = open_database(self, "import")
        self.store([versions[0][0]], arrival=1, conn=conn)
        lower = self.store([versions[1][0]], arrival=2, conn=conn)
        self.assertEqual(lower.unchanged, 1)
        self.assertEqual(self.rows(conn)[0]["revision_at"], "2026-10-02T10:00:00.900Z")

    def test_an_arrival_time_without_decimals_is_stored_with_three(self):
        self.store([changed(STEPS, revision_at=None)], arrival=1)
        self.assertEqual(self.rows()[0]["revision_at"], "2026-10-01T08:00:00.000Z")

        later_in_the_same_second = changed(STEPS, revision_at="2026-10-01T08:00:00.500Z")
        result = self.store([later_in_the_same_second], arrival=2)

        self.assertEqual(result.replaced, 1)

    def test_the_same_revision_is_replaced_only_by_a_different_reader_version(self):
        self.store([STEPS])
        same = self.store([STEPS], reader_version="r1")
        other = self.store(
            [changed(STEPS, values={"value": 2500, "unit": "count"})], reader_version="r2"
        )

        self.assertEqual((same.unchanged, other.replaced), (1, 1))
        self.assertEqual(self.value_of(), 2500)

    def test_a_replaced_session_swaps_its_parts_and_marks_both_dates(self):
        self.store([SESSION], arrival=1)
        record_id = self.rows()[0]["id"]
        self.conn.execute(
            "INSERT INTO session_groups (id, local_date, start_utc, chosen_json)"
            " VALUES ('g1', '2026-10-02', '2026-10-02T09:00:00Z', '{}')"
        )
        self.conn.execute(
            "INSERT INTO session_group_members (group_id, record_id) VALUES ('g1', ?)", (record_id,)
        )
        self.conn.execute("DELETE FROM rebuild_dates")
        moved = changed(
            SESSION,
            local_date="2026-10-03",
            revision_at="2026-10-02T12:00:00.000Z",
            parts=[{"seq": 7, "part_kind": "lap", "duration_s": 60.0}],
        )

        result = self.store([moved], arrival=2)

        parts = self.conn.execute("SELECT record_id, seq, part_kind FROM session_parts").fetchall()
        self.assertEqual([tuple(part) for part in parts], [(record_id, 7, "lap")])
        members = self.conn.execute("SELECT record_id FROM session_group_members").fetchall()
        self.assertEqual([member[0] for member in members], [record_id])
        dates = {"2026-10-02", "2026-10-03"}
        self.assertEqual(result.dates, dates)
        saved = self.conn.execute("SELECT local_date FROM rebuild_dates").fetchall()
        self.assertEqual({row[0] for row in saved}, dates)

    def test_a_record_with_only_utc_gets_the_local_date_of_each_instant(self):
        utc_only = changed(STEPS, local_date=None, utc_offset_min=None)
        records = [
            changed(utc_only, key="before", start_utc="2026-04-04T11:30:00Z"),
            changed(utc_only, key="after", start_utc="2026-04-06T11:30:00Z"),
        ]

        self.store(records, tz=Auckland())

        found = {row["key"]: (row["local_date"], row["utc_offset_min"]) for row in self.rows()}
        self.assertEqual(found, {"before": ("2026-04-05", None), "after": ("2026-04-06", None)})

    def test_a_big_import_commits_in_batches_and_skips_a_bad_record(self):
        dates = ("2026-10-01", "2026-10-02", "2026-10-03")
        records = [changed(STEPS, key=f"k{n}", local_date=dates[n % 3]) for n in range(1201)]
        wrong_unit = changed(
            STEPS, key="bad", local_date="2026-12-31", values={"value": 5, "unit": "kcal"}
        )
        records.insert(600, wrong_unit)
        calls = []

        result = self.store(records, batch_size=500, before_batch=lambda: calls.append(1))

        self.assertEqual(result.inserted, 1201)
        self.assertEqual(len(self.rows()), 1201)
        self.assertEqual(result.rejected, {"values.unit for steps must be count": 1})
        self.assertEqual(len(calls), 3)
        saved = self.conn.execute("SELECT local_date FROM rebuild_dates").fetchall()
        self.assertEqual({row[0] for row in saved}, set(dates))

    def test_a_session_with_a_repeated_part_number_is_skipped(self):
        repeated = changed(
            SESSION,
            parts=[{"seq": 1, "part_kind": "set"}, {"seq": 1, "part_kind": "set"}],
        )

        result = self.store([repeated, STEPS])

        self.assertEqual(result.rejected, {"parts repeat seq 1": 1})
        self.assertEqual([row["key"] for row in self.rows()], ["k1"])
