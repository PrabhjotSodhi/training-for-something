"""Reads an Apple Health export (export.zip or export.xml) into measurement records."""

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
    """Write the measurements in one Apple Health export to out_path as JSON Lines and return the result."""
    skipped: Counter = Counter()
    count = 0
    try:
        if fmt != "file":
            raise ValueError(f"format must be file, got {fmt!r}")
        with _open_export(path) as export, Path(out_path).open("w", encoding="utf-8") as out:
            for record in _measurements(export, skipped):
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


def _measurements(export: BinaryIO, skipped: Counter) -> Iterator[dict]:
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
        if element.tag == "Record":
            try:
                record = _measurement(element)
            except _SkipError as skip:
                skipped[skip.reason] += 1
            else:
                if record is not None:
                    yield record
        # Clearing a child on its own would empty a Record before it is read.
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
        value, unit = _quantity(element, raw_value)
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


def _quantity(element: ElementTree.Element, raw_value: str) -> tuple[float, str]:
    try:
        number = float(raw_value)
    except ValueError:
        raise _SkipError(_NOT_A_NUMBER) from None
    if not math.isfinite(number):
        raise _SkipError(_NOT_A_NUMBER)
    try:
        return convert(number, element.get("unit"))
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
