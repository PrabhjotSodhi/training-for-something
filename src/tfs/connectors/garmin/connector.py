"""Fetches Garmin Personal API data with a fresh token each run and reads saved pages into records."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tfs.connectors.core.contract import parse_iso_time, to_utc_text

_ORIGIN_APP = "com.garmin.connect.mobile"
_API = "https://personal.connectapi.garmin.com"
_TOKEN_URL = f"{_API}/personalapitoken-service/personal-api/personal-api-key/token"
_MAX_DAYS = 28
_OVERLAP = timedelta(days=1)
_DEFAULT_WAIT_S = 300
_PAGE_SIZE = 99
_RESULT_KEYS = {"hrv": "hrvSummaries", "resting_hr": "data", "weight": "dailyWeightSummaries"}
_ACTIVITIES = (
    "/activitylist-service/activities/search/activities"
    "?startDate={start}&endDate={end}&limit={limit}&start={offset}"
)
_PATHS = {
    "sleep": "/sleep-service/sleep/dailySleepSummariesByDate?startDate={start}&endDate={end}",
    "hrv": "/hrv-service/hrv/daily/{start}/{end}",
    "steps": "/usersummary-service/stats/steps/daily/{start}/{end}",
    "resting_hr": "/usersummary-service/v2/dailysummaries?startDate={start}&endDate={end}",
    "weight": "/weight-service/weight/range/{start}/{end}",
    "vo2_max": "/metrics-service/metrics/maxmet/daily/{start}/{end}",
}
_FAMILY_BY_TYPE = {
    "running": "run",
    "trail_running": "run",
    "cycling": "ride",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "strength_training": "strength",
    "walking": "walk_hike",
    "hiking": "walk_hike",
}


def connect(answers: dict) -> dict:
    """Turn the typed API key into the secrets the engine stores."""
    api_key = answers.get("api_key", "").strip()
    if not api_key:
        raise ValueError("Enter your Garmin Personal API key.")
    return {"secrets": {"api_key": api_key}}


def fetch(cursor: dict | None, secrets: dict, out_dir: str, http, now: str) -> dict:
    """Exchange the key for a token, download each data set since the cursor and return the files and next cursor."""
    now_time = parse_iso_time(now)
    earliest = now_time - timedelta(days=_MAX_DAYS)
    since = max(parse_iso_time(cursor["since"]), earliest) if cursor else earliest
    dates = {"start": since.date().isoformat(), "end": now_time.date().isoformat()}
    # The token is Garmin's reply body. It lives in this run's memory only.
    status, reply_headers, body = http(
        _TOKEN_URL, {"Authorization": f"ApiKey {secrets['api_key']}"}, method="POST"
    )
    if status != 200:
        return _stopped(status, reply_headers, cursor, now_time, "the token")
    token = body.decode("utf-8").strip()
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    files = []
    offset, more = 0, True
    while more:
        url = _API + _ACTIVITIES.format(**dates, limit=_PAGE_SIZE, offset=offset)
        status, reply_headers, body = http(url, headers)
        if status != 200:
            return _stopped(status, reply_headers, cursor, now_time, "activities")
        activities = json.loads(body)
        if activities != []:
            files.append(_save(out_dir, "activities", len(files) + 1, body))
        more = len(activities) == _PAGE_SIZE
        offset += _PAGE_SIZE
    for fmt, path in _PATHS.items():
        status, reply_headers, body = http(_API + path.format(**dates), headers)
        if status != 200:
            return _stopped(status, reply_headers, cursor, now_time, fmt)
        if not _empty_page(fmt, body):
            files.append(_save(out_dir, fmt, len(files) + 1, body))
    next_cursor = {"since": to_utc_text(now_time - _OVERLAP)}
    return {"files": files, "cursor": next_cursor, "auth_failed": False, "wait_until": None}


def _empty_page(fmt: str, body: bytes) -> bool:
    try:
        page = json.loads(body)
    except ValueError:
        return False  # Keep unreadable pages for the reader to report.
    if isinstance(page, dict) and fmt in _RESULT_KEYS:
        page = page.get(_RESULT_KEYS[fmt])
    return page == []


def _save(out_dir: str, fmt: str, number: int, body: bytes) -> dict:
    path = Path(out_dir) / f"{fmt}-{number}.json"
    path.write_bytes(body)
    return {"path": str(path), "fmt": fmt, "ext": "json"}


def _stopped(
    status: int, headers: dict, cursor: dict | None, now_time: datetime, what: str
) -> dict:
    result = {"files": [], "cursor": cursor, "auth_failed": False, "wait_until": None}
    if status in (401, 403):
        return result | {"auth_failed": True}
    if status == 429:
        value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
        wait = int(value) if value.isascii() and value.isdigit() else _DEFAULT_WAIT_S
        return result | {"wait_until": to_utc_text(now_time + timedelta(seconds=wait))}
    raise ValueError(f"Garmin answered status {status} for {what}.")


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write the records from one saved Garmin page to out_path as JSON Lines and return the result."""
    try:
        if fmt not in _READERS:
            raise ValueError(f"unknown format {fmt!r}")
        records = _READERS[fmt](json.loads(Path(path).read_text(encoding="utf-8")))
    except (KeyError, TypeError, ValueError) as error:
        return {
            "status": "unreadable",
            "count": 0,
            "message": f"TFS cannot read this Garmin page: {error!r}",
        }
    lines = "".join(json.dumps(record) + "\n" for record in records)
    Path(out_path).write_text(lines, encoding="utf-8")
    return {"status": "ok", "count": len(records), "message": None}


def _from_ms(milliseconds: int | str) -> datetime:
    return datetime.fromtimestamp(int(milliseconds) / 1000, UTC)


def _from_text(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _offset_min(local: datetime, utc: datetime) -> int:
    return int((local - utc).total_seconds() // 60)


def _record(kind: str, stream: str, key: str, start: datetime, end: datetime | None, **fields):
    return {
        "kind": kind,
        "stream": stream,
        "key": key,
        "revision_at": None,
        "start_utc": to_utc_text(start),
        "end_utc": end and to_utc_text(end),
        "utc_offset_min": None,
        "local_date": start.date().isoformat(),
        "label": None,
        "family": None,
        "origin_app": _ORIGIN_APP,
        "origin_device": None,
        "plan_ref": None,
    } | fields


def _measurement(
    stream: str, date: str, metric: str, value: float, unit: str, start=None, end=None, **fields
) -> dict:
    # Daily readings carry no time of day, so they start at midnight of their date.
    start = start or datetime.fromisoformat(date).replace(tzinfo=UTC)
    return _record(
        "measurement",
        stream,
        date,
        start,
        end,
        metric=metric,
        values={"value": value, "unit": unit},
        local_date=date,
        **fields,
    )


def _activity(item: dict) -> dict:
    start = _from_text(item["startTimeGMT"])
    local = _from_text(item["startTimeLocal"])
    values = {
        "duration_s": item["duration"],
        "distance_m": item.get("distance"),
        "active_energy_kcal": item.get("calories"),
        "avg_hr": item.get("averageHR"),
        "max_hr": item.get("maxHR"),
    }
    return _record(
        "session",
        "activities",
        str(item["activityId"]),
        start,
        start + timedelta(seconds=item["duration"]),
        utc_offset_min=_offset_min(local, start),
        local_date=local.date().isoformat(),
        label=item["activityName"],
        family=_FAMILY_BY_TYPE.get(item["activityType"]["typeKey"]),
        values={name: number for name, number in values.items() if number is not None},
    )


def _sleep(page: list[dict]) -> list[dict]:
    records = []
    for item in page:
        if item.get("sleepTimeSeconds") is None:
            continue
        start = _from_ms(item["sleepStartTimestampGMT"])
        total = _measurement(
            "sleep",
            item["calendarDate"],
            "sleep_asleep",
            item["sleepTimeSeconds"] / 3600,
            "h",
            start=start,
            end=_from_ms(item["sleepEndTimestampGMT"]),
            utc_offset_min=_offset_min(_from_ms(item["sleepStartTimestampLocal"]), start),
        )
        records.append(total)
        for field, metric in (
            ("deepSleepSeconds", "sleep_deep"),
            ("lightSleepSeconds", "sleep_core"),
            ("remSleepSeconds", "sleep_rem"),
            ("awakeSleepSeconds", "sleep_awake"),
        ):
            if (seconds := item.get(field)) is not None:
                records.append(
                    total
                    | {
                        "key": f"{total['key']}|{metric}",
                        "metric": metric,
                        "values": {
                            "value": seconds / 3600,
                            "unit": "h",
                            "sleep_summary_key": total["key"],
                        },
                    }
                )
    return records


def _daily(stream: str, metric: str, unit: str, items: list[dict], value_of) -> list[dict]:
    # Garmin leaves a day's value out when the device recorded nothing, so that day gets no record.
    return [
        _measurement(stream, item["calendarDate"], metric, value, unit)
        for item in items
        if (value := value_of(item)) is not None
    ]


def _weight(item: dict) -> dict:
    latest = item["latestWeight"]
    return _measurement(
        "weight",
        latest["calendarDate"],
        "weight",
        latest["weight"],
        "kg",
        start=_from_ms(latest["timestampGMT"]),
    )


_READERS = {
    "activities": lambda page: [_activity(item) for item in page],
    "sleep": _sleep,
    "hrv": lambda page: _daily(
        "hrv", "hrv_rmssd", "ms", page["hrvSummaries"], lambda item: item.get("lastNightAvg")
    ),
    "steps": lambda page: _daily(
        "steps", "steps", "count", page, lambda item: item.get("totalSteps")
    ),
    "resting_hr": lambda page: _daily(
        "resting_hr",
        "resting_hr",
        "bpm",
        page["data"],
        lambda item: (item.get("heartRate") or {}).get("restingValue"),
    ),
    "weight": lambda page: [
        _weight(item)
        for item in page["dailyWeightSummaries"]
        if (item.get("latestWeight") or {}).get("weight") is not None
    ],
    "vo2_max": lambda page: _daily(
        "vo2_max",
        "vo2_max",
        "mL/kg/min",
        [item["generic"] for item in page if item.get("generic")],
        lambda item: item.get("vo2MaxValue"),
    ),
}
