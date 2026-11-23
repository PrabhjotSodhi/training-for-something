"""Reads a Health Auto Export REST push into measurement and session records."""

import json
import math
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from tfs.connectors.core.contract import METRICS, RecordError, convert, parse_local_time, utc_fields

_STREAM = "metrics"
_ORIGIN_APP = "Health Auto Export"
_NOT_A_PAYLOAD = "Not a Health Auto Export payload."
_METRIC_BY_NAME = {
    "step_count": "steps",
    "active_energy": "active_energy",
    "resting_heart_rate": "resting_hr",
    "weight_body_mass": "weight",
    "body_fat_percentage": "body_fat",
    "vo2_max": "vo2_max",
    "heart_rate_variability": "hrv_sdnn",
    "heart_rate": "heart_rate",
}
_NOT_A_NUMBER = "with a value that is not a number"
_UNREADABLE_TIME = "with a time that TFS cannot read"
_UNREADABLE_UNIT = "with a unit or size that TFS cannot read"
_NO_ID = "without an id"
_WORKOUT_STREAM = "workouts"
_FAMILIES_BY_NAME = {
    "run": ("Outdoor Run", "Indoor Run", "Running"),
    "ride": ("Outdoor Cycle", "Indoor Cycle", "Cycling"),
    "swim": ("Pool Swim", "Open Water Swim", "Swimming"),
    "strength": ("Traditional Strength Training", "Functional Strength Training"),
    "row": ("Rowing",),
    "walk_hike": ("Outdoor Walk", "Indoor Walk", "Walking", "Hiking"),
    "mixed": ("High Intensity Interval Training",),
    "mobility": ("Yoga", "Pilates", "Flexibility"),
}
_FAMILY_BY_NAME = {name: family for family, names in _FAMILIES_BY_NAME.items() for name in names}
_NOT_AGGREGATED = "Turn on Aggregate Sleep Data in Health Auto Export."


class _SkipError(Exception):
    """A record that cannot be imported."""


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write the measurements and workouts in one Health Auto Export push to out_path as JSON Lines and return the result."""
    skipped: Counter = Counter()
    try:
        if fmt != "rest_json":
            raise ValueError(f"format must be rest_json, got {fmt!r}")
        metrics, workouts = _payload(path)
        records = [*_measurements(metrics, skipped), *_sessions(workouts, skipped)]
    except ValueError as error:
        Path(out_path).write_text("", encoding="utf-8")
        return {"status": "unreadable", "count": 0, "message": str(error)}
    Path(out_path).write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    message = " ".join(
        _NOT_AGGREGATED
        if reason == _NOT_AGGREGATED
        else f"Skipped {number} {noun}{'' if number == 1 else 's'} {reason}."
        for (noun, reason), number in skipped.items()
    )
    return {
        "status": "partial" if skipped else "ok",
        "count": len(records),
        "message": message or None,
    }


def _payload(path: str) -> tuple[list[dict], list]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))["data"]
    except (ValueError, KeyError, TypeError):
        raise ValueError(_NOT_A_PAYLOAD) from None
    if not isinstance(data, dict) or not ("metrics" in data or "workouts" in data):
        raise ValueError(_NOT_A_PAYLOAD)
    metrics = data.get("metrics", [])
    workouts = data.get("workouts", [])
    if (
        not isinstance(metrics, list)
        or not all(isinstance(entry, dict) for entry in metrics)
        or not isinstance(workouts, list)
    ):
        raise ValueError(_NOT_A_PAYLOAD)
    return metrics, workouts


def _measurements(metrics: list[dict], skipped: Counter) -> Iterator[dict]:
    for entry in metrics:
        name = entry.get("name")
        if name not in _METRIC_BY_NAME and name != "sleep_analysis":
            continue
        samples = entry.get("data")
        if not isinstance(samples, list):
            raise ValueError(_NOT_A_PAYLOAD)
        for sample in samples:
            try:
                yield _measurement(name, entry.get("units"), sample)
            except _SkipError as skip:
                skipped["sample", str(skip)] += 1


def _sessions(workouts: list, skipped: Counter) -> Iterator[dict]:
    for workout in workouts:
        try:
            yield _session(workout)
        except _SkipError as skip:
            skipped["workout", str(skip)] += 1


def _session(workout: object) -> dict:
    workout_id = workout.get("id") if isinstance(workout, dict) else None
    if not isinstance(workout_id, str) or not workout_id:
        raise _SkipError(_NO_ID)
    start, end = _local_time(workout.get("start")), _local_time(workout.get("end"))
    name = workout.get("name")
    name = name if isinstance(name, str) else None
    start_utc, offset_min, local_date = utc_fields(start)
    return {
        "kind": "session",
        "stream": _WORKOUT_STREAM,
        "key": workout_id,
        "revision_at": None,
        "start_utc": start_utc,
        "end_utc": utc_fields(end)[0],
        "utc_offset_min": offset_min,
        "local_date": local_date,
        "label": name,
        "family": _FAMILY_BY_NAME.get(name),
        "values": {
            key: value for key, value in _workout_values(workout).items() if value is not None
        },
        "origin_app": None,
        "origin_device": None,
        "plan_ref": None,
    }


def _workout_values(workout: dict) -> dict[str, float | None]:
    heart_rate = workout.get("heartRate")
    if heart_rate is None:
        heart_rate = {}
    elif not isinstance(heart_rate, dict):
        raise _SkipError(_NOT_A_NUMBER)
    return {
        "duration_s": _workout_number(workout.get("duration"), "s", "s"),
        "distance_m": _workout_quantity(workout.get("distance"), "m"),
        "active_energy_kcal": _workout_quantity(workout.get("activeEnergyBurned"), "kcal"),
        "avg_hr": _workout_quantity(heart_rate.get("avg", workout.get("avgHeartRate")), "bpm"),
        "max_hr": _workout_quantity(heart_rate.get("max", workout.get("maxHeartRate")), "bpm"),
        "effort_rating": _workout_quantity(workout.get("effortScore"), "effort"),
        "effort_estimate": _workout_quantity(workout.get("estimatedEffortScore"), "effort"),
    }


def _workout_quantity(raw: object, target: str) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _SkipError(_NOT_A_NUMBER)
    return _workout_number(raw.get("qty"), raw.get("units"), target)


def _workout_number(raw: object, units: object, target: str) -> float | None:
    return None if raw is None else _number(raw, units, target)


def _measurement(name: str, units: object, sample: object) -> dict:
    if not isinstance(sample, dict):
        raise _SkipError(_NOT_A_NUMBER)
    if name == "sleep_analysis":
        return _sleep(units, sample)
    metric = _METRIC_BY_NAME[name]
    target = METRICS[metric].unit
    text = sample.get("date")
    start = _local_time(text)
    if metric == "heart_rate":
        values = {"value": _number(sample.get("Avg"), units, target)}
        for bound in ("min", "max"):
            if bound.capitalize() in sample:
                values[bound] = _number(sample[bound.capitalize()], units, target)
    else:
        values = {"value": _number(sample.get("qty"), units, target)}
    values["unit"] = target
    start_utc, offset_min, local_date = utc_fields(start)
    return _record(name, text, sample, metric, values, (start_utc, None, offset_min, local_date))


def _sleep(units: object, sample: dict) -> dict:
    if not sample.get("sleepStart") or not sample.get("sleepEnd"):
        raise _SkipError(_NOT_AGGREGATED)
    start, end = _local_time(sample["sleepStart"]), _local_time(sample["sleepEnd"])
    total = sample.get("totalSleep")
    value = _number(sample.get("asleep") if total is None else total, units, "h")
    start_utc, offset_min, _ = utc_fields(start)
    # Sleep that ends at 18:00 or later belongs to the next day (HLD 5.6).
    local_date = (end + timedelta(hours=6)).date().isoformat()
    when = (start_utc, utc_fields(end)[0], offset_min, local_date)
    return _record(
        "sleep_analysis",
        sample["sleepStart"],
        sample,
        "sleep_asleep",
        {"value": value, "unit": "h"},
        when,
    )


def _record(name: str, key_time: str, sample: dict, metric: str, values: dict, when: tuple) -> dict:
    start_utc, end_utc, offset_min, local_date = when
    source = sample.get("source")
    source = source if isinstance(source, str) and source else None
    if source and "|" in source:
        values["merged"] = True
    return {
        "kind": "measurement",
        "stream": _STREAM,
        "key": f"{name}|{source or '-'}|{key_time}",
        "revision_at": None,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "utc_offset_min": offset_min,
        "local_date": local_date,
        "label": None,
        "family": None,
        "metric": metric,
        "values": values,
        "origin_app": _ORIGIN_APP,
        "origin_device": source,
        "plan_ref": None,
    }


def _local_time(text: object) -> datetime:
    try:
        return parse_local_time(text)
    except (TypeError, ValueError):
        raise _SkipError(_UNREADABLE_TIME) from None


def _number(raw: object, units: object, target: str) -> float:
    if not isinstance(raw, int | float) or isinstance(raw, bool) or not math.isfinite(raw):
        raise _SkipError(_NOT_A_NUMBER)
    try:
        value, unit = convert(raw, units)
    except (RecordError, TypeError):
        raise _SkipError(_UNREADABLE_UNIT) from None
    if unit != target:
        raise _SkipError(_UNREADABLE_UNIT)
    return round(value, 7)
