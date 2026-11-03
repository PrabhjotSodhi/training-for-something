"""Reads an Apple Health export (export.zip or export.xml) into measurement and session records."""

import json
import math
import re
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree

from tfs.connectors.core.contract import METRICS, convert, parse_local_time, utc_fields

_STREAM = "records"
_METRIC_BY_TYPE = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr",
    "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
}
_WORKOUT_STREAM = "workouts"
_ACTIVITY_PREFIX = "HKWorkoutActivityType"
_ACTIVITIES_BY_FAMILY = {
    "run": ("Running",),
    "ride": ("Cycling",),
    "swim": ("Swimming",),
    "strength": ("TraditionalStrengthTraining", "FunctionalStrengthTraining"),
    "row": ("Rowing",),
    "walk_hike": ("Walking", "Hiking"),
    "ski": ("DownhillSkiing", "CrossCountrySkiing", "SnowSports"),
    "racket": ("Tennis", "Squash", "Badminton", "TableTennis", "Pickleball", "Racquetball"),
    "combat": ("Boxing", "MartialArts", "Kickboxing", "Wrestling"),
    "climb": ("Climbing",),
    "mixed": ("HighIntensityIntervalTraining", "CrossTraining", "MixedCardio"),
    "mobility": ("Yoga", "Flexibility", "Pilates", "Cooldown"),
}
_FAMILY_BY_ACTIVITY = {
    activity: family
    for family, activities in _ACTIVITIES_BY_FAMILY.items()
    for activity in activities
}
_DISTANCE_TYPES = tuple(
    f"HKQuantityTypeIdentifier{name}"
    for name in ("DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming")
)
_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
_HEART_RATE_TYPE = "HKQuantityTypeIdentifierHeartRate"
_NO_STATISTIC = ElementTree.Element("WorkoutStatistics")
_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_ASLEEP_VALUES = {
    f"HKCategoryValueSleepAnalysis{stage}"
    for stage in ("AsleepUnspecified", "AsleepCore", "AsleepDeep", "AsleepREM", "Asleep")
}
_DEVICE_NAME = re.compile(r"name:([^,>]*)")
_NOT_A_NUMBER = "a value that is not a number"
_UNREADABLE_FIELD = "a unit or time that TFS cannot read"
_DAMAGED = (zipfile.BadZipFile, zlib.error, EOFError, ElementTree.ParseError)


class _SkipError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write the measurements and workouts in one Apple Health export to out_path as JSON Lines and return the result."""
    skipped: Counter = Counter()
    count = 0
    try:
        if fmt != "file":
            raise ValueError(f"format must be file, got {fmt!r}")
        with _open_export(path) as export, Path(out_path).open("w", encoding="utf-8") as out:
            for record in _records(export, skipped):
                out.write(json.dumps(record) + "\n")
                count += 1
    except _DAMAGED as error:
        return _unreadable(out_path, f"The export is incomplete or damaged: {error}.")
    except ValueError as error:
        return _unreadable(out_path, str(error))
    message = " ".join(
        f"Skipped {number} record{'' if number == 1 else 's'} with {reason}."
        for reason, number in skipped.items()
    )
    return {"status": "partial" if skipped else "ok", "count": count, "message": message or None}


def _unreadable(out_path: str, message: str) -> dict:
    Path(out_path).write_text("", encoding="utf-8")
    return {"status": "unreadable", "count": 0, "message": message}


@contextmanager
def _open_export(path: str) -> Iterator[BinaryIO]:
    if Path(path).suffix.lower() != ".zip":
        with Path(path).open("rb") as export:
            yield export
        return
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.lower().endswith("export.xml") and "cda" not in name.lower()
        ]
        if not names:
            raise ValueError("The zip has no export.xml inside.")
        with archive.open(names[0]) as export:
            yield export


def _records(export: BinaryIO, skipped: Counter) -> Iterator[dict]:
    root = None
    depth = 0
    for event, element in ElementTree.iterparse(export, events=("start", "end")):
        if event == "start":
            root = element if root is None else root
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue
        if element.tag in ("Record", "Workout"):
            try:
                record = _measurement(element) if element.tag == "Record" else _session(element)
            except _SkipError as skip:
                skipped[skip.reason] += 1
            else:
                if record is not None:
                    yield record
        # Clearing a child on its own would empty a Record or Workout before it is read.
        root.clear()


def _measurement(element: ElementTree.Element) -> dict | None:
    type_ = element.get("type")
    raw_value = element.get("value", "")
    if type_ == _SLEEP_TYPE:
        metric = "sleep_asleep" if raw_value in _ASLEEP_VALUES else None
    else:
        metric = _METRIC_BY_TYPE.get(type_)
    if metric is None:
        return None
    if metric == "sleep_asleep":
        start, end = _times(element)
        value, unit = round((end - start).total_seconds() / 3600, 7), "h"
    else:
        value, unit = _quantity(raw_value, element.get("unit"))
        start, end = _times(element)
    if unit != METRICS[metric].unit:
        raise _SkipError(_UNREADABLE_FIELD)
    start_utc, offset_min, local_date = utc_fields(start)
    if metric == "sleep_asleep":
        # Sleep that ends at 18:00 or later belongs to the next day (HLD 5.6).
        local_date = (end + timedelta(hours=6)).date().isoformat()
    source = element.get("sourceName", "")
    key = "|".join((type_, source, element.get("startDate"), element.get("endDate"), raw_value))
    device = _DEVICE_NAME.search(element.get("device", ""))
    return {
        "kind": "measurement",
        "stream": _STREAM,
        "key": key,
        "revision_at": None,
        "start_utc": start_utc,
        "end_utc": utc_fields(end)[0],
        "utc_offset_min": offset_min,
        "local_date": local_date,
        "label": None,
        "family": None,
        "metric": metric,
        "values": {"value": value, "unit": unit},
        "origin_app": source,
        "origin_device": device.group(1).strip() if device else None,
        "plan_ref": None,
    }


def _session(element: ElementTree.Element) -> dict:
    type_ = element.get("workoutActivityType", "")
    activity = type_.removeprefix(_ACTIVITY_PREFIX)
    start, end = _times(element)
    source = element.get("sourceName", "")
    key = "|".join((type_, source, element.get("startDate"), element.get("endDate")))
    device = _DEVICE_NAME.search(element.get("device", ""))
    start_utc, offset_min, local_date = utc_fields(start)
    values = _workout_values(element)
    return {
        "kind": "session",
        "stream": _WORKOUT_STREAM,
        "key": key,
        "revision_at": None,
        "start_utc": start_utc,
        "end_utc": utc_fields(end)[0],
        "utc_offset_min": offset_min,
        "local_date": local_date,
        "label": activity,
        "family": _FAMILY_BY_ACTIVITY.get(activity),
        "values": {name: number for name, number in values.items() if number is not None},
        "origin_app": source,
        "origin_device": device.group(1).strip() if device else None,
        "plan_ref": None,
    }


def _workout_values(element: ElementTree.Element) -> dict[str, float | None]:
    statistics = {stat.get("type"): stat for stat in element.iterfind("WorkoutStatistics")}
    distance = next((statistics[t] for t in _DISTANCE_TYPES if t in statistics), _NO_STATISTIC)
    energy = statistics.get(_ENERGY_TYPE, _NO_STATISTIC)
    heart_rate = statistics.get(_HEART_RATE_TYPE, _NO_STATISTIC)
    heart_unit = heart_rate.get("unit")
    return {
        "duration_s": _amount(element.get("duration"), element.get("durationUnit"), "s"),
        "distance_m": _statistic_or_total(distance, element, "totalDistance", "m"),
        "active_energy_kcal": _statistic_or_total(energy, element, "totalEnergyBurned", "kcal"),
        "avg_hr": _amount(heart_rate.get("average"), heart_unit, "bpm"),
        "max_hr": _amount(heart_rate.get("maximum"), heart_unit, "bpm"),
    }


def _statistic_or_total(
    statistic: ElementTree.Element,
    workout: ElementTree.Element,
    total: str,
    target: str,
) -> float | None:
    value = _amount(statistic.get("sum"), statistic.get("unit"), target)
    if value is None:
        value = _amount(workout.get(total), workout.get(f"{total}Unit"), target)
    return value


def _amount(raw_value: str | None, unit: str | None, target: str) -> float | None:
    if raw_value is None:
        return None
    value, converted_unit = _quantity(raw_value, unit)
    if converted_unit != target:
        raise _SkipError(_UNREADABLE_FIELD)
    return value


def _quantity(raw_value: str, unit: str | None) -> tuple[float, str]:
    try:
        number = float(raw_value)
    except ValueError:
        raise _SkipError(_NOT_A_NUMBER) from None
    if not math.isfinite(number):
        raise _SkipError(_NOT_A_NUMBER)
    try:
        return convert(number, unit)
    except ValueError:
        raise _SkipError(_UNREADABLE_FIELD) from None


def _times(element: ElementTree.Element) -> tuple[datetime, datetime]:
    try:
        return (
            parse_local_time(element.get("startDate", "")),
            parse_local_time(element.get("endDate", "")),
        )
    except ValueError:
        raise _SkipError(_UNREADABLE_FIELD) from None
