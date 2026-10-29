"""The record shapes every connector returns and the checks the engine runs on them."""

import hashlib
import re
import sys
from datetime import UTC, date, datetime
from typing import NamedTuple

FAMILIES = (
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
)


class Metric(NamedTuple):
    """One measurement type: its unit, how a day is summed up and whether 0 means no reading."""

    unit: str
    daily: str
    zero_is_missing: bool


METRICS = {
    "steps": Metric("count", "sum", False),
    "active_energy": Metric("kcal", "sum", False),
    "sleep_asleep": Metric("h", "sum", True),
    "resting_hr": Metric("bpm", "latest", True),
    "weight": Metric("kg", "latest", False),
    "heart_rate": Metric("bpm", "none", True),
    "hrv_sdnn": Metric("ms", "none", True),
}

SESSION_VALUES = ("duration_s", "distance_m", "active_energy_kcal", "avg_hr", "max_hr")

TIMED_KINDS = ("session", "measurement", "event", "document")
KINDS = (*TIMED_KINDS, "deleted")

_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_REVISION = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_LOCAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}")

_UNITS = {
    "kJ": ("kcal", lambda value: value / 4.184),
    "kcal": ("kcal", lambda value: value),
    "Cal": ("kcal", lambda value: value),
    "lb": ("kg", lambda value: value * 0.45359237),
    "st": ("kg", lambda value: value * 6.35029318),
    "g": ("kg", lambda value: value / 1000),
    "kg": ("kg", lambda value: value),
    "km": ("m", lambda value: value * 1000),
    "mi": ("m", lambda value: value * 1609.344),
    "m": ("m", lambda value: value),
    "min": ("s", lambda value: value * 60),
    "s": ("s", lambda value: value),
    "hr": ("h", lambda value: value),
    "h": ("h", lambda value: value),
    "count/min": ("bpm", lambda value: value),
    "bpm": ("bpm", lambda value: value),
    "count": ("count", lambda value: value),
    "ms": ("ms", lambda value: value),
}


class RecordError(ValueError):
    """A record does not match the contract. The message says which field and why."""


def validate_record(rec: dict) -> dict:
    """Return rec unchanged if it matches the contract, otherwise raise RecordError."""
    if not isinstance(rec, dict):
        raise RecordError("record must be a dict")
    kind = rec.get("kind")
    if kind not in KINDS:
        raise RecordError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    for field in ("stream", "key"):
        if not isinstance(rec.get(field), str) or not rec[field]:
            raise RecordError(f"{field} must be a non-empty string")
    if rec.get("revision_at") is not None:
        _instant("revision_at", rec["revision_at"], _REVISION, "2026-10-20T19:06:12.000Z")
    if kind == "deleted":
        return rec
    _check_times(rec)
    if rec.get("family") is not None and rec["family"] not in FAMILIES:
        raise RecordError(f"family must be null or one of {', '.join(FAMILIES)}")
    if kind == "measurement":
        _check_measurement(rec)
    elif kind == "session":
        _check_session(rec)
    return rec


def record_id(connector: str, stream: str, key: str) -> str:
    """Return the stable id of a record: sha256 hex of connector, stream and key."""
    return hashlib.sha256("\x1f".join((connector, stream, key)).encode("utf-8")).hexdigest()


def parse_local_time(text: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS +HHMM' into an aware datetime, or raise ValueError."""
    if not _LOCAL_TIME.fullmatch(text):
        raise ValueError(f"time must look like 2026-03-01 07:12:33 +1300, got {text!r}")
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")


def parse_iso_time(text: str) -> datetime:
    """Parse ISO 8601 with Z or an offset into an aware datetime, or raise ValueError."""
    parsed = datetime.fromisoformat(text)
    if parsed.utcoffset() is None:
        raise ValueError(f"time needs Z or an offset like +13:00, got {text!r}")
    return parsed


def utc_fields(dt: datetime) -> tuple[str, int, str]:
    """Return the UTC text, the UTC offset in minutes and the local date of an aware datetime."""
    offset = _require_aware(dt).utcoffset()
    return to_utc_text(dt), int(offset.total_seconds() // 60), dt.date().isoformat()


def to_utc_text(dt: datetime) -> str:
    """Return the instant as UTC text without fractional seconds, like 2026-02-28T18:12:33Z."""
    return _require_aware(dt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_revision_text(dt: datetime) -> str:
    """Return the instant as UTC text with 3 decimal places, like 2026-02-28T18:12:33.250Z."""
    utc = _require_aware(dt).astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def convert(value: float, unit: str) -> tuple[float, str]:
    """Return value in the unit TFS stores, or raise RecordError for an unknown unit."""
    if unit not in _UNITS:
        raise RecordError(f"unit {unit!r} is not supported, use one of {', '.join(_UNITS)}")
    target, change = _UNITS[unit]
    return float(change(value)), target


def _require_aware(dt: datetime) -> datetime:
    if dt.utcoffset() is None:
        raise ValueError("datetime needs a time zone")
    return dt


def _instant(field: str, value: object, pattern: re.Pattern, example: str) -> datetime:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RecordError(f"{field} must look like {example}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise RecordError(f"{field} is not a real date and time: {value}") from error


def _check_times(rec: dict) -> None:
    example = "2026-02-28T18:12:33Z"
    start = _instant("start_utc", rec.get("start_utc"), _INSTANT, example)
    if rec.get("end_utc") is not None:
        end = _instant("end_utc", rec["end_utc"], _INSTANT, example)
        if end < start:
            raise RecordError("end_utc must not be before start_utc")
    _check_local_time(rec.get("utc_offset_min"), rec.get("local_date"))


def _check_local_time(offset: object, local_date: object) -> None:
    if offset is not None and (
        not isinstance(offset, int) or isinstance(offset, bool) or not -720 <= offset <= 840
    ):
        raise RecordError("utc_offset_min must be null or a whole number from -720 to 840")
    if local_date is None:
        if offset is not None:
            raise RecordError("local_date is required when utc_offset_min is set")
        return
    if not isinstance(local_date, str) or not _LOCAL_DATE.fullmatch(local_date):
        raise RecordError("local_date must look like 2026-03-01")
    try:
        date.fromisoformat(local_date)
    except ValueError as error:
        raise RecordError(f"local_date is not a real date: {local_date}") from error


def _is_finite_number(value: object) -> bool:
    # Comparing to the largest float never overflows, and it is False for nan and inf.
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and abs(value) <= sys.float_info.max
    )


def _check_measurement(rec: dict) -> None:
    name = rec.get("metric")
    metric = METRICS.get(name) if isinstance(name, str) else None
    if metric is None:
        raise RecordError(f"metric must be one of {', '.join(METRICS)}")
    values = rec.get("values")
    if not isinstance(values, dict):
        raise RecordError("values must be a dict")
    if not _is_finite_number(values.get("value")):
        raise RecordError("values.value must be a finite number")
    if values.get("unit") != metric.unit:
        raise RecordError(f"values.unit for {rec['metric']} must be {metric.unit}")
    if "merged" in values and not isinstance(values["merged"], bool):
        raise RecordError("values.merged must be true or false")
    for bound in ("min", "max"):
        if bound in values and not _is_finite_number(values[bound]):
            raise RecordError(f"values.{bound} must be a finite number")


def _check_session(rec: dict) -> None:
    values = rec.get("values")
    if not isinstance(values, dict):
        raise RecordError("values must be a dict")
    for name, number in values.items():
        if name not in SESSION_VALUES:
            raise RecordError(f"values.{name} is not a session value")
        if not _is_finite_number(number):
            raise RecordError(f"values.{name} must be a finite number")
    parts = rec.get("parts", [])
    if not isinstance(parts, list):
        raise RecordError("parts must be a list")
    for part in parts:
        seq = part.get("seq") if isinstance(part, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise RecordError("each item in parts needs a whole-number seq")
        if not isinstance(part.get("part_kind"), str):
            raise RecordError("each item in parts needs a string part_kind")
