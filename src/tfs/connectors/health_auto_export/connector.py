"""Reads a Health Auto Export REST push into measurement records."""

import json
import math
from collections import Counter
from collections.abc import Iterator
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
    "heart_rate_variability": "hrv_sdnn",
    "heart_rate": "heart_rate",
}
_NOT_A_NUMBER = "a value that is not a number"
_UNREADABLE_TIME = "a time that TFS cannot read"
_UNREADABLE_UNIT = "a unit or size that TFS cannot read"


class _SkipError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write the measurements in one Health Auto Export push to out_path as JSON Lines and return the result."""
    skipped: Counter = Counter()
    try:
        if fmt != "rest_json":
            raise ValueError(f"format must be rest_json, got {fmt!r}")
        records = list(_measurements(_metrics(path), skipped))
    except ValueError as error:
        Path(out_path).write_text("", encoding="utf-8")
        return {"status": "unreadable", "count": 0, "message": str(error)}
    Path(out_path).write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    message = " ".join(
        f"Skipped {number} sample{'' if number == 1 else 's'} with {reason}."
        for reason, number in skipped.items()
    )
    return {
        "status": "partial" if skipped else "ok",
        "count": len(records),
        "message": message or None,
    }


def _metrics(path: str) -> list[dict]:
    try:
        metrics = json.loads(Path(path).read_text(encoding="utf-8"))["data"]["metrics"]
    except (ValueError, KeyError, TypeError):
        raise ValueError(_NOT_A_PAYLOAD) from None
    if not isinstance(metrics, list) or not all(isinstance(entry, dict) for entry in metrics):
        raise ValueError(_NOT_A_PAYLOAD)
    return metrics


def _measurements(metrics: list[dict], skipped: Counter) -> Iterator[dict]:
    for entry in metrics:
        name = entry.get("name")
        if name not in _METRIC_BY_NAME:
            continue
        samples = entry.get("data")
        if not isinstance(samples, list):
            raise ValueError(_NOT_A_PAYLOAD)
        for sample in samples:
            try:
                yield _measurement(name, entry.get("units"), sample)
            except _SkipError as skip:
                skipped[skip.reason] += 1


def _measurement(name: str, units: object, sample: object) -> dict:
    if not isinstance(sample, dict):
        raise _SkipError(_NOT_A_NUMBER)
    metric = _METRIC_BY_NAME[name]
    target = METRICS[metric].unit
    text = sample.get("date")
    try:
        start = parse_local_time(text)
    except (TypeError, ValueError):
        raise _SkipError(_UNREADABLE_TIME) from None
    if metric == "heart_rate":
        values = {"value": _number(sample.get("Avg"), units, target)}
        for bound in ("min", "max"):
            if bound.capitalize() in sample:
                values[bound] = _number(sample[bound.capitalize()], units, target)
    else:
        values = {"value": _number(sample.get("qty"), units, target)}
    values["unit"] = target
    source = sample.get("source")
    source = source if isinstance(source, str) and source else None
    if source and "|" in source:
        values["merged"] = True
    start_utc, offset_min, local_date = utc_fields(start)
    return {
        "kind": "measurement",
        "stream": _STREAM,
        "key": f"{name}|{source or '-'}|{text}",
        "revision_at": None,
        "start_utc": start_utc,
        "end_utc": None,
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
