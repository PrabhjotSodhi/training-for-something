import json
import unittest

from tests.helpers import insert_file, insert_workout, open_database
from tfs.duplicates.workouts import update

MANIFESTS = {
    "hevy": {"app_ids": ["com.hevyapp.hevy", "Hevy"]},
    "apple_health": {"app_ids": ["com.apple.Health"]},
    "health_auto_export": {"app_ids": []},
}


class DuplicateValuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(self)
        insert_file(self.conn, 1, "2026-10-01T00:00:00Z")

    def add(
        self,
        record_id: str,
        connector: str,
        values: dict,
        *,
        family: str | None = "run",
        parts: bool = False,
        **fields,
    ) -> None:
        insert_workout(self.conn, record_id, connector, values=values, family=family, **fields)
        if parts:
            self.conn.execute(
                "INSERT INTO session_parts (record_id, seq, part_kind) VALUES (?, 1, 'set')",
                (record_id,),
            )

    def chosen(self) -> dict:
        update(self.conn, {"2026-10-01"}, MANIFESTS)
        (found,) = self.conn.execute("SELECT chosen_json FROM session_groups").fetchall()
        return json.loads(found[0])

    def test_an_original_with_parts_supplies_what_it_has_and_the_copy_the_rest(self) -> None:
        self.add(
            "hevy-1", "hevy", {"duration_s": 3900}, label="Push A", family="strength", parts=True
        )
        self.add(
            "apple-1",
            "apple_health",
            {"duration_s": 3902, "active_energy_kcal": 250, "avg_hr": 118},
            family="strength",
            origin_app="Hevy",
        )

        chosen = self.chosen()

        self.assertEqual(
            chosen,
            {
                "label": {"value": "Push A", "record_id": "hevy-1"},
                "family": {"value": "strength", "record_id": "hevy-1"},
                "duration_s": {"value": 3900, "record_id": "hevy-1"},
                "active_energy_kcal": {"value": 250, "record_id": "apple-1"},
                "avg_hr": {"value": 118, "record_id": "apple-1"},
            },
        )
        self.assertEqual(
            self.conn.execute("SELECT family FROM session_groups").fetchone()[0], "strength"
        )

    def test_two_originals_give_distance_and_duration_from_the_earlier_arrival(self) -> None:
        self.add("later", "apple_health", {"distance_m": 8100, "duration_s": 2470}, arrival_seq=5)
        self.add("earlier", "hevy", {"distance_m": 8020, "duration_s": 2460}, arrival_seq=3)

        chosen = self.chosen()

        self.assertEqual(chosen["distance_m"], {"value": 8020, "record_id": "earlier"})
        self.assertEqual(chosen["duration_s"], {"value": 2460, "record_id": "earlier"})

    def test_distance_and_duration_come_from_a_record_that_has_both(self) -> None:
        self.add("distance-only", "hevy", {"distance_m": 5000}, arrival_seq=1)
        self.add("both", "apple_health", {"distance_m": 5010, "duration_s": 1800}, arrival_seq=2)

        chosen = self.chosen()

        self.assertEqual(chosen["distance_m"], {"value": 5010, "record_id": "both"})
        self.assertEqual(chosen["duration_s"], {"value": 1800, "record_id": "both"})

    def test_equal_rank_goes_to_the_smaller_id_in_either_insert_order(self) -> None:
        results = []
        for order in (("a", "b"), ("b", "a")):
            self.conn.execute("DELETE FROM source_records")
            self.conn.execute("DELETE FROM session_groups")
            connectors = {"a": "hevy", "b": "apple_health"}
            for record_id in order:
                self.add(
                    record_id,
                    connectors[record_id],
                    {"distance_m": 100 if record_id == "a" else 101, "duration_s": 60},
                )
            results.append(self.chosen())

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["distance_m"], {"value": 100, "record_id": "a"})

    def test_a_field_the_distance_record_lacks_stays_missing(self) -> None:
        self.add("distance-only", "hevy", {"distance_m": 5000}, arrival_seq=1)
        self.add("duration-only", "apple_health", {"duration_s": 1800}, arrival_seq=2)

        chosen = self.chosen()

        self.assertEqual(chosen["distance_m"], {"value": 5000, "record_id": "distance-only"})
        self.assertNotIn("duration_s", chosen)


if __name__ == "__main__":
    unittest.main()
