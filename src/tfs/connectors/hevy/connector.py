"""Fetches Hevy workout pages and reads saved pages into session records and delete records."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from tfs.connectors.core.contract import parse_iso_time, to_revision_text, to_utc_text
from tfs.connectors.hevy import routines

_STREAM = "workouts"
_ORIGIN_APP = "com.hevyapp.hevy"
_API = "https://api.hevyapp.com/v1"
_PAGE_SIZE = 10
_TEMPLATE_PAGE_SIZE = 100
_OVERLAP = timedelta(minutes=5)
_DEFAULT_WAIT_S = 300

send = routines.send


def connect(answers: dict) -> dict:
    """Turn the typed API key into the secrets the engine stores."""
    api_key = answers.get("api_key", "").strip()
    if not api_key:
        raise ValueError("Enter your Hevy API key.")
    return {"secrets": {"api_key": api_key}}


class _StopError(Exception):
    def __init__(self, result: dict) -> None:
        self.result = result


def fetch(cursor: dict | None, secrets: dict, out_dir: str, http, now: str) -> dict:
    """Download every workout page, or the events since the cursor, plus the routine list and each exercise's muscle."""
    now_time = parse_iso_time(now)
    if cursor is None:
        fmt, url = "workouts_page", f"{_API}/workouts?page={{page}}&pageSize={_PAGE_SIZE}"
    else:
        since = quote(cursor["since"], safe="")
        fmt = "events_page"
        url = f"{_API}/workouts/events?page={{page}}&pageSize={_PAGE_SIZE}&since={since}"
    headers = {"api-key": secrets["api_key"], "accept": "application/json"}
    files = []
    try:
        for page, body in enumerate(_pages(http, url, headers, cursor, now_time), start=1):
            path = Path(out_dir) / f"{fmt}-{page}.json"
            path.write_bytes(body)
            files.append({"path": str(path), "fmt": fmt, "ext": "json"})
        routines_url = f"{_API}/routines?page={{page}}&pageSize={_PAGE_SIZE}"
        targets = [
            {
                "remote_id": routine["id"],
                "title": routine["title"],
                "folder": routine.get("folder_id"),
                "remote_version": routine["updated_at"],
            }
            for body in _pages(http, routines_url, headers, cursor, now_time)
            for routine in json.loads(body)["routines"]
        ]
        templates_url = f"{_API}/exercise_templates?page={{page}}&pageSize={_TEMPLATE_PAGE_SIZE}"
        muscles = {
            template["id"]: template["primary_muscle_group"]
            for body in _pages(http, templates_url, headers, cursor, now_time)
            for template in json.loads(body)["exercise_templates"]
            if template.get("primary_muscle_group")
        }
    except _StopError as stop:
        return stop.result
    result = _fetch_result(files, {"since": to_utc_text(now_time - _OVERLAP)})
    return {**result, "targets": targets, "muscles": muscles}


def _pages(http, url: str, headers: dict, cursor: dict | None, now_time) -> Iterator[bytes]:
    page, page_count = 1, 1
    while page <= page_count:
        status, reply_headers, body = http(url.format(page=page), headers)
        if status in (401, 403):
            raise _StopError(_fetch_result([], cursor, auth_failed=True))
        if status == 429:
            wait = _retry_after_seconds(reply_headers)
            wait_until = to_utc_text(now_time + timedelta(seconds=wait))
            raise _StopError(_fetch_result([], cursor, wait_until=wait_until))
        if status != 200:
            raise ValueError(f"Hevy answered status {status} for page {page}.")
        if page == 1:
            page_count = _page_count(body)
        yield body
        page += 1


def _fetch_result(
    files: list[dict],
    cursor: dict | None,
    *,
    auth_failed: bool = False,
    wait_until: str | None = None,
) -> dict:
    return {"files": files, "cursor": cursor, "auth_failed": auth_failed, "wait_until": wait_until}


def _retry_after_seconds(headers: dict) -> int:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    return int(value) if value.isascii() and value.isdigit() else _DEFAULT_WAIT_S


def _page_count(body: bytes) -> int:
    try:
        return int(json.loads(body)["page_count"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Hevy page 1 has no page_count.") from None


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write the records from one saved Hevy page to out_path as JSON Lines and return the result."""
    try:
        if fmt not in ("workouts_page", "events_page"):
            raise ValueError(f"format must be workouts_page or events_page, got {fmt!r}")
        page = json.loads(Path(path).read_text(encoding="utf-8"))
        if fmt == "workouts_page":
            records = [_session(workout) for workout in page["workouts"]]
        else:
            records = [_event_record(event) for event in page["events"]]
    except KeyError as error:
        return _unreadable(f"the page is missing {error}")
    except (TypeError, ValueError) as error:
        return _unreadable(str(error))
    lines = "".join(json.dumps(record) + "\n" for record in records)
    Path(out_path).write_text(lines, encoding="utf-8")
    return {"status": "ok", "count": len(records), "message": None}


def _unreadable(reason: str) -> dict:
    return {
        "status": "unreadable",
        "count": 0,
        "message": f"TFS cannot read this Hevy page: {reason}",
    }


def _event_record(event: dict) -> dict:
    if event["type"] == "updated":
        return _session(event["workout"])
    if event["type"] == "deleted":
        return {
            "kind": "deleted",
            "stream": _STREAM,
            "key": event["id"],
            "revision_at": to_revision_text(parse_iso_time(event["deleted_at"])),
        }
    raise ValueError(f"unknown event type {event['type']!r}")


def _session(workout: dict) -> dict:
    start = parse_iso_time(workout["start_time"])
    end = parse_iso_time(workout["end_time"])
    return {
        "kind": "session",
        "stream": _STREAM,
        "key": workout["id"],
        "revision_at": to_revision_text(parse_iso_time(workout["updated_at"])),
        "start_utc": to_utc_text(start),
        "end_utc": to_utc_text(end),
        "utc_offset_min": None,
        "local_date": None,
        "label": workout["title"],
        "family": "strength",
        "values": {"duration_s": (end - start).total_seconds()},
        "origin_app": _ORIGIN_APP,
        "origin_device": None,
        "plan_ref": workout.get("routine_id"),
        "parts": _parts(workout["exercises"]),
    }


def _parts(exercises: list[dict]) -> list[dict]:
    parts = []
    for exercise in exercises:
        for set_ in exercise["sets"]:
            parts.append(
                {
                    "seq": len(parts) + 1,
                    "part_kind": "set",
                    "label": exercise["title"],
                    "reps": set_.get("reps"),
                    "weight_kg": set_.get("weight_kg"),
                    "duration_s": set_.get("duration_seconds"),
                    "distance_m": set_.get("distance_meters"),
                    "extras": {
                        "set_type": set_.get("type"),
                        "rpe": set_.get("rpe"),
                        "exercise_index": exercise["index"],
                        "set_index": set_["index"],
                        "exercise_template_id": exercise["exercise_template_id"],
                    },
                }
            )
    return parts
