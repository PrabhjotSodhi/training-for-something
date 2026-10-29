import copy
import hashlib
import unittest

from tfs.connectors.core.contract import (
    FAMILIES,
    METRICS,
    SESSION_VALUES,
    RecordError,
    record_id,
    validate_record,
)

MEASUREMENT = {
    "kind": "measurement",
    "stream": "records",
    "key": "k1",
    "revision_at": None,
    "start_utc": "2026-02-28T18:12:33Z",
    "end_utc": "2026-02-28T18:20:00Z",
    "utc_offset_min": 780,
    "local_date": "2026-03-01",
    "label": "HKQuantityTypeIdentifierStepCount",
    "family": None,
    "metric": "steps",
    "values": {"value": 532.0, "unit": "count"},
    "origin_app": "Ana's Apple Watch",
    "origin_device": "Apple Watch",
    "plan_ref": None,
}

SESSION = {
    "kind": "session",
    "stream": "workouts",
    "key": "w-001",
    "revision_at": "2026-10-20T19:06:12.000Z",
    "start_utc": "2026-10-20T18:00:00Z",
    "end_utc": "2026-10-20T19:05:00Z",
    "utc_offset_min": None,
    "local_date": None,
    "label": "Push A",
    "family": "strength",
    "metric": None,
    "values": {"duration_s": 3900.0},
    "origin_app": "com.hevyapp.hevy",
    "origin_device": None,
    "plan_ref": "r-010",
    "parts": [
        {
            "seq": 1,
            "part_kind": "set",
            "label": "Bench Press",
            "reps": 6,
            "weight_kg": 80.0,
            "duration_s": None,
            "distance_m": None,
            "avg_hr": None,
            "extras": {"set_type": "normal"},
        }
    ],
}

DELETED = {
    "kind": "deleted",
    "stream": "workouts",
    "key": "w-002",
    "revision_at": "2026-10-21T08:00:00.000Z",
}


def changed(rec, **fields):
    """Return a copy of rec with some fields replaced."""
    copied = copy.deepcopy(rec)
    copied.update(fields)
    return copied


class ValidateAcceptTest(unittest.TestCase):
    def test_accepts_the_three_examples(self):
        for rec in (MEASUREMENT, SESSION, DELETED):
            with self.subTest(kind=rec["kind"]):
                self.assertEqual(validate_record(copy.deepcopy(rec)), rec)

    def test_accepts_a_deleted_record_without_revision(self):
        rec = {"kind": "deleted", "stream": "workouts", "key": "w-003"}
        self.assertEqual(validate_record(rec), rec)

    def test_accepts_event_and_document_kinds(self):
        for kind in ("event", "document"):
            with self.subTest(kind=kind):
                rec = changed(MEASUREMENT, kind=kind, metric=None, values={})
                self.assertEqual(validate_record(rec), rec)

    def test_accepts_every_family_and_metric(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                validate_record(changed(SESSION, family=family))
        for metric, spec in METRICS.items():
            with self.subTest(metric=metric):
                validate_record(
                    changed(MEASUREMENT, metric=metric, values={"value": 1, "unit": spec.unit})
                )

    def test_accepts_merged_min_and_max_on_a_measurement(self):
        values = {"value": 62.0, "unit": "bpm", "merged": True, "min": 55, "max": 71.5}
        validate_record(changed(MEASUREMENT, metric="heart_rate", values=values))

    def test_accepts_an_open_ended_record(self):
        validate_record(changed(MEASUREMENT, end_utc=None))

    def test_accepts_equal_start_and_end(self):
        validate_record(changed(SESSION, end_utc=SESSION["start_utc"]))


class ValidateRejectTest(unittest.TestCase):
    def assert_rejected(self, rec, word):
        with self.assertRaises(RecordError) as caught:
            validate_record(rec)
        self.assertIn(word, str(caught.exception))

    def test_rejects_a_wrong_unit(self):
        rec = changed(MEASUREMENT, values={"value": 532.0, "unit": "kcal"})
        self.assert_rejected(rec, "unit")

    def test_rejects_a_bool_value(self):
        rec = changed(MEASUREMENT, values={"value": True, "unit": "count"})
        self.assert_rejected(rec, "value")

    def test_rejects_a_value_that_is_not_finite(self):
        for bad in (float("nan"), float("inf"), "532"):
            with self.subTest(bad=bad):
                rec = changed(MEASUREMENT, values={"value": bad, "unit": "count"})
                self.assert_rejected(rec, "value")

    def test_rejects_an_unknown_family(self):
        self.assert_rejected(changed(MEASUREMENT, family="running"), "family")

    def test_rejects_an_unknown_metric(self):
        self.assert_rejected(changed(MEASUREMENT, metric="glucose"), "metric")

    def test_rejects_a_session_that_ends_before_it_starts(self):
        rec = changed(SESSION, end_utc="2026-10-20T17:59:59Z")
        self.assert_rejected(rec, "end_utc")

    def test_rejects_a_revision_without_milliseconds(self):
        rec = changed(SESSION, revision_at="2026-10-20T19:06:12Z")
        self.assert_rejected(rec, "revision_at")

    def test_rejects_a_start_that_is_not_a_real_instant(self):
        for bad in ("2026-13-01T00:00:00Z", "2026-02-28 18:12:33", "2026-02-28T18:12:33.000Z"):
            with self.subTest(bad=bad):
                self.assert_rejected(changed(MEASUREMENT, start_utc=bad), "start_utc")

    def test_rejects_a_deleted_record_with_an_empty_key(self):
        self.assert_rejected(changed(DELETED, key=""), "key")

    def test_rejects_an_empty_stream(self):
        self.assert_rejected(changed(DELETED, stream=""), "stream")

    def test_rejects_a_complete_list(self):
        rec = {"kind": "complete_list", "stream": "workouts", "key": "all"}
        self.assert_rejected(rec, "kind")

    def test_rejects_a_record_that_is_not_a_dict(self):
        self.assert_rejected(["measurement"], "record")

    def test_rejects_an_offset_out_of_range_or_not_whole(self):
        for bad in (-721, 841, 60.5, True, "60"):
            with self.subTest(bad=bad):
                rec = changed(MEASUREMENT, utc_offset_min=bad)
                self.assert_rejected(rec, "utc_offset_min")

    def test_rejects_an_offset_without_a_local_date(self):
        self.assert_rejected(changed(MEASUREMENT, local_date=None), "local_date")

    def test_rejects_a_local_date_that_is_not_a_date(self):
        self.assert_rejected(changed(MEASUREMENT, local_date="2026-02-30"), "local_date")

    def test_rejects_session_values_outside_the_list(self):
        self.assert_rejected(changed(SESSION, values={"calories": 10.0}), "calories")

    def test_rejects_session_values_that_are_not_numbers(self):
        for bad in (True, "3900", float("nan")):
            with self.subTest(bad=bad):
                self.assert_rejected(changed(SESSION, values={"duration_s": bad}), "duration_s")

    def test_rejects_a_part_without_a_whole_seq_or_a_kind(self):
        for part in ({"seq": 1.5, "part_kind": "set"}, {"seq": 1}, {"seq": 1, "part_kind": 3}):
            with self.subTest(part=part):
                self.assert_rejected(changed(SESSION, parts=[part]), "parts")

    def test_rejects_parts_that_is_not_a_list(self):
        for bad in (None, 3):
            with self.subTest(bad=bad):
                with self.assertRaises(RecordError) as caught:
                    validate_record(changed(SESSION, parts=bad))
                self.assertEqual(str(caught.exception), "parts must be a list")

    def test_rejects_a_metric_that_is_not_a_string(self):
        with self.assertRaises(RecordError) as caught:
            validate_record(changed(MEASUREMENT, metric=["steps"]))
        self.assertEqual(str(caught.exception), f"metric must be one of {', '.join(METRICS)}")

    def test_rejects_a_measurement_extra_that_is_not_valid(self):
        for values in (
            {"value": 1.0, "unit": "count", "merged": "yes"},
            {"value": 1.0, "unit": "count", "min": True},
            {"value": 1.0, "unit": "count", "max": float("inf")},
        ):
            with self.subTest(values=values):
                self.assert_rejected(changed(MEASUREMENT, values=values), "values")


class ConstantsTest(unittest.TestCase):
    def test_families(self):
        self.assertEqual(
            FAMILIES,
            (
                "run",
                "ride",
                "swim",
                "strength",
                "row",
                "walk_hike",
                "ski",
                "racket",
                "combat",
                "climb",
                "mixed",
                "mobility",
            ),
        )

    def test_metrics(self):
        table = {
            name: (metric.unit, metric.daily, metric.zero_is_missing)
            for name, metric in METRICS.items()
        }
        self.assertEqual(
            table,
            {
                "steps": ("count", "sum", False),
                "active_energy": ("kcal", "sum", False),
                "sleep_asleep": ("h", "sum", True),
                "resting_hr": ("bpm", "latest", True),
                "weight": ("kg", "latest", False),
                "heart_rate": ("bpm", "none", True),
                "hrv_sdnn": ("ms", "none", True),
            },
        )

    def test_session_values(self):
        self.assertEqual(
            SESSION_VALUES,
            ("duration_s", "distance_m", "active_energy_kcal", "avg_hr", "max_hr"),
        )


class RecordIdTest(unittest.TestCase):
    def test_hashes_the_three_parts_with_unit_separators(self):
        expected = hashlib.sha256(b"hevy\x1fworkouts\x1fabc").hexdigest()
        self.assertEqual(record_id("hevy", "workouts", "abc"), expected)

    def test_differs_when_a_part_moves_between_fields(self):
        self.assertNotEqual(record_id("a", "bc", "d"), record_id("ab", "c", "d"))


if __name__ == "__main__":
    unittest.main()
