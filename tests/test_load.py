import json
import unittest

from tests.helpers import FIRST_FILE, insert_file, insert_workout, open_database
from tfs import scores
from tfs.importer.session import rebuild_pending


class StrengthLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        insert_file(self.conn, FIRST_FILE["arrival_seq"], FIRST_FILE["revision_at"])

    def workout(self, record_id: str, day: str, sets: list[tuple], connector: str = "hevy") -> None:
        """Add a strength workout; each set is (reps, weight_kg, set_type) or with a fourth rpe."""
        insert_workout(
            self.conn, record_id, connector, start=f"{day}T06:00:00Z", end=f"{day}T07:00:00Z"
        )
        for seq, (reps, weight_kg, set_type, *rpe) in enumerate(sets):
            self.conn.execute(
                "INSERT INTO session_parts (record_id, seq, part_kind, reps, weight_kg, extras_json)"
                " VALUES (?, ?, 'set', ?, ?, ?)",
                (
                    record_id,
                    seq,
                    reps,
                    weight_kg,
                    json.dumps({"set_type": set_type, "rpe": rpe[0] if rpe else None}),
                ),
            )

    def load_rows(self) -> dict[str, tuple[int, dict]]:
        self.conn.execute(
            "INSERT OR IGNORE INTO rebuild_dates SELECT local_date FROM source_records"
        )
        rebuild_pending(self.conn)
        return {
            day: (value, json.loads(inputs))
            for day, value, inputs in self.conn.execute(
                "SELECT local_date, value, inputs_json FROM scores WHERE score = 'load_strength'"
            )
        }

    def test_warm_up_sets_are_left_out(self) -> None:
        # GIVEN 3 working sets of 5 x 100 kg and a warm-up of 5 x 60 kg
        self.workout("a", "2026-03-02", [(5, 60, "warmup")] + [(5, 100, "normal")] * 3)

        # WHEN the scores are rebuilt
        rows = self.load_rows()

        # THEN only the working sets count
        self.assertEqual(rows["2026-03-02"][0], 1500)

    def test_a_linked_copy_from_another_app_counts_once(self) -> None:
        # GIVEN the same workout from Hevy and from Apple Health, both with sets
        self.workout("a", "2026-03-02", [(5, 100, "normal")] * 3)
        self.workout("b", "2026-03-02", [(5, 100, "normal")] * 3, connector="apple_health")

        # WHEN the scores are rebuilt
        rows = self.load_rows()

        # THEN the load is still 1,500
        self.assertEqual(rows["2026-03-02"][0], 1500)

    def test_a_day_without_strength_sets_has_no_row(self) -> None:
        # GIVEN a workout with no sets on one day and a set on another
        insert_workout(
            self.conn, "a", "hevy", start="2026-03-01T06:00:00Z", end="2026-03-01T07:00:00Z"
        )
        self.workout("b", "2026-03-02", [(5, 100, "normal")])

        # WHEN the scores are rebuilt
        rows = self.load_rows()

        # THEN only the day with sets has a row
        self.assertEqual(list(rows), ["2026-03-02"])

    def test_the_normal_is_the_median_of_the_previous_28_days_with_work(self) -> None:
        # GIVEN loads of 100, 300, 200 on days inside the window, 900 outside it, and a body weight set today
        self.workout("old", "2026-01-30", [(1, 900, "normal")])
        for record_id, day, weight in (
            ("a", "2026-02-10", 100),
            ("b", "2026-02-20", 300),
            ("c", "2026-03-01", 200),
        ):
            self.workout(record_id, day, [(1, weight, "normal")])
        self.workout("today", "2026-03-02", [(5, 100, "normal", 8), (10, None, "normal")])

        # WHEN the scores are rebuilt
        value, inputs = self.load_rows()["2026-03-02"]

        # THEN the row shows today against the median of the three days in the window
        self.assertEqual(value, 500)
        self.assertEqual((inputs["normal"], inputs["normal_days"]), (200, 3))
        self.assertEqual((inputs["mean_rpe"], inputs["note"]), (8, "body weight sets not counted"))

    def test_a_stored_row_with_an_older_formula_version_is_rebuilt(self) -> None:
        # GIVEN a workout whose load row carries an old formula version
        self.workout("a", "2026-03-02", [(5, 100, "normal")])
        self.load_rows()
        self.conn.execute("UPDATE scores SET formula_version = 'old'")

        # WHEN the outdated check runs
        scores.rebuild_if_outdated(self.conn)

        # THEN the row is recomputed
        self.assertNotEqual(
            self.conn.execute("SELECT formula_version FROM scores").fetchone()[0], "old"
        )
