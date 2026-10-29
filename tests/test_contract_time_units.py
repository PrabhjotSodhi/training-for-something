import unittest
from datetime import UTC, datetime, timedelta, timezone

from tfs.connectors.core.contract import (
    RecordError,
    convert,
    parse_iso_time,
    parse_local_time,
    to_revision_text,
    to_utc_text,
    utc_fields,
    validate_record,
)


class TimeTests(unittest.TestCase):
    def test_local_time_gives_utc_offset_and_local_date(self):
        parsed = parse_local_time("2026-03-01 07:12:33 +1300")
        self.assertEqual(utc_fields(parsed), ("2026-02-28T18:12:33Z", 780, "2026-03-01"))

    def test_local_time_with_negative_offset(self):
        parsed = parse_local_time("2026-03-01 23:30:00 -0830")
        self.assertEqual(utc_fields(parsed), ("2026-03-02T08:00:00Z", -510, "2026-03-01"))

    def test_local_time_must_have_date_time_and_offset(self):
        for text in (
            "2026-03-01",
            "2026-03-01 07:12:33",
            "2026-03-01T07:12:33 +1300",
            "2026-03-01 07:12:33 +13:00",
            "2026-02-30 07:12:33 +1300",
            "",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_local_time(text)

    def test_iso_time_with_z_and_with_offset_are_equal(self):
        with_z = parse_iso_time("2026-10-20T19:06:12.345Z")
        with_offset = parse_iso_time("2026-10-20T19:06:12.345+00:00")
        self.assertEqual(with_z, with_offset)

    def test_utc_text_drops_fractional_seconds(self):
        parsed = parse_iso_time("2026-10-20T19:06:12.345Z")
        self.assertEqual(to_utc_text(parsed), "2026-10-20T19:06:12Z")

    def test_revision_text_keeps_three_decimal_places(self):
        parsed = parse_iso_time("2026-10-20T19:06:12.345Z")
        self.assertEqual(to_revision_text(parsed), "2026-10-20T19:06:12.345Z")
        whole = parse_iso_time("2026-10-20T19:06:12Z")
        self.assertEqual(to_revision_text(whole), "2026-10-20T19:06:12.000Z")

    def test_iso_time_without_fraction_and_with_offset(self):
        parsed = parse_iso_time("2026-10-21T08:06:12+13:00")
        self.assertEqual(utc_fields(parsed), ("2026-10-20T19:06:12Z", 780, "2026-10-21"))

    def test_iso_time_without_offset_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_iso_time("2026-10-20T19:06:12")

    def test_text_helpers_convert_to_utc_without_the_computer_time_zone(self):
        parsed = datetime(2026, 3, 1, 7, 12, 33, 999000, tzinfo=timezone(timedelta(hours=13)))
        self.assertEqual(to_utc_text(parsed), "2026-02-28T18:12:33Z")
        self.assertEqual(to_revision_text(parsed), "2026-02-28T18:12:33.999Z")

    def test_years_before_1000_keep_four_digits(self):
        first = datetime(1, 1, 1, tzinfo=UTC)
        self.assertEqual(to_utc_text(first), "0001-01-01T00:00:00Z")
        self.assertEqual(
            to_revision_text(first.replace(microsecond=250000)), "0001-01-01T00:00:00.250Z"
        )
        self.assertEqual(
            to_utc_text(datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC)), "0999-12-31T23:59:59Z"
        )

    def test_text_for_early_years_passes_the_record_check(self):
        moment = datetime(1, 1, 1, tzinfo=UTC)
        validate_record(
            {"kind": "event", "stream": "s", "key": "k", "start_utc": to_utc_text(moment)}
        )
        validate_record(
            {"kind": "deleted", "stream": "s", "key": "k", "revision_at": to_revision_text(moment)}
        )

    def test_naive_datetimes_are_rejected(self):
        naive = datetime.fromisoformat("2026-03-01T07:12:33")
        for function in (utc_fields, to_utc_text, to_revision_text):
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                function(naive)


class ConvertTests(unittest.TestCase):
    def assert_converts(self, value, unit, expected, expected_unit):
        result, result_unit = convert(value, unit)
        self.assertAlmostEqual(result, expected, places=7)
        self.assertEqual(result_unit, expected_unit)

    def test_energy(self):
        self.assert_converts(418.4, "kJ", 100.0, "kcal")
        self.assert_converts(250, "kcal", 250.0, "kcal")
        self.assert_converts(250, "Cal", 250.0, "kcal")

    def test_mass(self):
        self.assert_converts(154, "lb", 69.85322498, "kg")
        self.assert_converts(11, "st", 69.85322498, "kg")
        self.assert_converts(2500, "g", 2.5, "kg")
        self.assert_converts(70, "kg", 70.0, "kg")

    def test_distance(self):
        self.assertEqual(convert(5.2, "km"), (5200.0, "m"))
        self.assert_converts(3.1, "mi", 4988.9664, "m")
        self.assertEqual(convert(800, "m"), (800.0, "m"))

    def test_time(self):
        self.assertEqual(convert(90, "min"), (5400.0, "s"))
        self.assertEqual(convert(7.5, "hr"), (7.5, "h"))
        self.assertEqual(convert(7.5, "h"), (7.5, "h"))
        self.assertEqual(convert(45, "s"), (45.0, "s"))
        self.assertEqual(convert(42, "ms"), (42.0, "ms"))

    def test_counts_and_rates(self):
        self.assertEqual(convert(58, "count/min"), (58.0, "bpm"))
        self.assertEqual(convert(58, "bpm"), (58.0, "bpm"))
        self.assertEqual(convert(532, "count"), (532.0, "count"))

    def test_unknown_unit_raises_record_error_naming_it(self):
        with self.assertRaises(RecordError) as caught:
            convert(1, "furlong")
        self.assertIn("furlong", str(caught.exception))

    def test_results_that_overflow_are_rejected(self):
        for value, unit in ((10**308, "min"), (1e308, "min"), (1e308, "km"), (-1e308, "km")):
            with self.subTest(value=value, unit=unit):
                with self.assertRaises(RecordError) as caught:
                    convert(value, unit)
                self.assertIn("too large", str(caught.exception))

    def test_text_and_booleans_are_not_numbers(self):
        for value, unit in (("90", "min"), (True, "kg")):
            with self.subTest(value=value):
                with self.assertRaises(RecordError) as caught:
                    convert(value, unit)
                self.assertEqual(
                    str(caught.exception),
                    f"value must be a finite number, got {value!r}. "
                    "Turn text into a number before converting.",
                )


if __name__ == "__main__":
    unittest.main()
