"""The record shapes every connector returns, the manifest every connector holds, and the
checks the engine runs on both.

A connector is a folder with a `manifest.json` and a module. The module may provide these
functions.

`read(path: str, fmt: str, out_path: str) -> dict`
    Writes the records from one raw file to `out_path` as JSON Lines. Returns
    `{"status": "ok" | "partial" | "unreadable", "count": int, "message": str | None}`.

`fetch(cursor: dict | None, secrets: dict, out_dir: str, http, now: str) -> dict`
    Downloads new raw files into `out_dir`. Returns `{"files": [{"path", "fmt", "ext"}],
    "cursor": dict | None, "auth_failed": bool, "wait_until": str | None}`.

`connect(answers: dict) -> dict`
    Turns what the user typed into `{"secrets": {...}}` for the engine to store.

`send` arrives in 0.3.
"""

import hashlib
import json
import math
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
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


_NOT_CONNECTORS = ("core", "testing")


class RecordError(ValueError):
    """A record does not match the contract. The message says which field and why."""


class ManifestError(ValueError):
    """A manifest does not match the contract. The message says which field and why."""


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
    return _utc_isoformat(dt, "seconds")


def to_revision_text(dt: datetime) -> str:
    """Return the instant as UTC text with 3 decimal places, like 2026-02-28T18:12:33.250Z."""
    return _utc_isoformat(dt, "milliseconds")


def convert(value: float, unit: str) -> tuple[float, str]:
    """Return value in the unit TFS stores, or raise RecordError for a bad number, an unknown unit or a result too large."""
    if not _is_finite_number(value):
        raise RecordError(
            f"value must be a finite number, got {value!r}. Turn text into a number before converting."
        )
    if unit not in _UNITS:
        raise RecordError(f"unit {unit!r} is not supported, use one of {', '.join(_UNITS)}")
    target, change = _UNITS[unit]
    try:
        converted = float(change(value))
    except OverflowError:
        converted = math.inf
    if not math.isfinite(converted):
        raise RecordError(f"{unit} value is too large to convert to {target}")
    return converted, target


def _utc_isoformat(dt: datetime, timespec: str) -> str:
    # The contract needs four digit years. strftime("%Y") drops the padding on glibc.
    return _require_aware(dt).astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


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
    seen = set()
    for part in parts:
        seq = part.get("seq") if isinstance(part, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise RecordError("each item in parts needs a whole-number seq")
        if not isinstance(part.get("part_kind"), str):
            raise RecordError("each item in parts needs a string part_kind")
        if seq in seen:
            raise RecordError(f"parts repeat seq {seq}")
        seen.add(seq)


def load_manifest(path: Path | str) -> dict:
    """Read one manifest.json and return it, or raise ManifestError naming the bad field."""
    path = Path(path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"{path}: cannot read the file: {error}") from error
    except ValueError as error:
        raise ManifestError(f"{path}: not valid JSON: {error}") from error
    try:
        _check_object("manifest", manifest, _MANIFEST_FIELDS)
    except ManifestError as error:
        raise ManifestError(f"{path}: {error}") from error
    return manifest


def load_manifests(root: Path | None = None) -> dict[str, dict]:
    """Read every <root>/<name>/manifest.json, except core and testing, keyed by folder name."""
    root = Path(__file__).resolve().parent.parent if root is None else root
    return {
        folder.name: load_manifest(folder / "manifest.json")
        for folder in sorted(root.iterdir())
        if folder.is_dir()
        and folder.name not in _NOT_CONNECTORS
        and (folder / "manifest.json").is_file()
    }


def _check_object(name: str, value: object, checks: dict) -> None:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    prefix = "" if name == "manifest" else f"{name}."
    for field in checks:
        if field not in value:
            raise ManifestError(f"{prefix}{field} is missing")
    for field in value:
        if field not in checks:
            raise ManifestError(f"{prefix}{field} is not a manifest field")
    for field, check in checks.items():
        check(f"{prefix}{field}", value[field])


def _check_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestError(f"{name} must be text")


def _check_flag(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ManifestError(f"{name} must be true or false")


def _check_list(name: str, value: object) -> list:
    if not isinstance(value, list):
        raise ManifestError(f"{name} must be a list")
    return value


def _check_text_list(name: str, value: object) -> None:
    for item in _check_list(name, value):
        _check_text(name, item)


def _check_hours(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ManifestError(f"{name} must be a whole number above 0")


def _check_families(name: str, value: object) -> None:
    for item in _check_list(name, value):
        if item not in FAMILIES:
            raise ManifestError(f"{name} holds {item!r}, use only {', '.join(FAMILIES)}")


def _check_asks(name: str, value: object) -> None:
    for index, ask in enumerate(_check_list(name, value)):
        _check_object(f"{name}[{index}]", ask, _ASK_FIELDS)


_ROUTE_FIELDS = {"fetch": _check_flag, "push": _check_flag, "files": _check_text_list}
_ASK_FIELDS = {"key": _check_text, "label": _check_text, "secret": _check_flag}
_MANIFEST_FIELDS = {
    "label": _check_text,
    "about": _check_text,
    "routes": lambda name, value: _check_object(name, value, _ROUTE_FIELDS),
    "asks": _check_asks,
    "stale_after_hours": _check_hours,
    "app_ids": _check_text_list,
    "notices_deletes": _check_flag,
    "sends": _check_families,
}
