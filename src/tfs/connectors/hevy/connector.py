"""Reads saved Hevy API pages into session records and delete records."""

import json
from pathlib import Path

from tfs.connectors.core.contract import parse_iso_time, to_revision_text, to_utc_text

_STREAM = "workouts"
_ORIGIN_APP = "com.hevyapp.hevy"


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
