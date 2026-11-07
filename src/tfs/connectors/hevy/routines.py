"""Hevy routine mapping and reconciliation; real writes await provider proof."""

import json
from collections.abc import Callable
from functools import partial
from http.client import HTTPException
from urllib.parse import quote

_WRITES_PROVEN = False
_API = "https://api.hevyapp.com/v1"
_SET_FIELDS = (
    "type",
    "weight_kg",
    "reps",
    "distance_meters",
    "duration_seconds",
    "custom_metric",
    "rep_range",
)
_EXERCISE_FIELDS = ("exercise_template_id", "superset_id", "rest_seconds", "notes")


class _DeliveryError(Exception):
    def __init__(self, status: str, message: str, **extra) -> None:
        self.result = {"status": status, "message": message, **extra}


def _exercises(parts: list, templates: list) -> list:
    names = {}
    for template in templates:
        names.setdefault(template["title"].strip().casefold(), []).append(template["id"])
    exercises = []
    for part in parts:
        allowed = {"part_kind", "label", "reps", "weight_kg", "distance_m", "duration_s"}
        kind = part["part_kind"]
        matches = names.get(part.get("label", "").strip().casefold(), [])
        if len(matches) != 1:
            raise _DeliveryError(
                "needs_you", "Use an exact, unambiguous Hevy exercise name for each set."
            )
        if part.keys() - allowed or kind not in ("set", "normal", "warmup", "dropset", "failure"):
            raise _DeliveryError(
                "needs_you", "This plan has targets Hevy delivery does not support yet."
            )
        values = {key: None for key in _SET_FIELDS}
        values.update(
            type="normal" if kind == "set" else kind,
            reps=part.get("reps"),
            weight_kg=part.get("weight_kg"),
        )
        for source, target in (
            ("distance_m", "distance_meters"),
            ("duration_s", "duration_seconds"),
        ):
            if source in part:
                number = part[source]
                if type(number) not in (int, float) or number < 0 or not float(number).is_integer():
                    raise _DeliveryError(
                        "needs_you",
                        "Hevy requires whole metres and seconds; change the target explicitly.",
                    )
                values[target] = int(number)
        exercise_id = matches[0]
        if not exercises or exercises[-1]["exercise_template_id"] != exercise_id:
            exercises.append(
                {
                    "exercise_template_id": exercise_id,
                    "superset_id": None,
                    "rest_seconds": None,
                    "notes": None,
                    "sets": [],
                }
            )
        exercises[-1]["sets"].append(values)
    if not exercises:
        raise _DeliveryError("needs_you", "Add strength sets before sending this routine.")
    return exercises


def _shape(routine: dict) -> dict:
    return {
        "title": routine["title"],
        "folder_id": routine.get("folder_id"),
        "exercises": [
            {
                **{key: exercise.get(key) for key in _EXERCISE_FIELDS},
                "sets": [
                    {key: entry.get(key) for key in _SET_FIELDS} for entry in exercise["sets"]
                ],
            }
            for exercise in routine["exercises"]
        ],
    }


def _confirmed(routine: dict, expected: dict) -> dict:
    actual = _shape(routine)
    if "notes" in expected:
        actual["notes"] = routine.get("notes")
    if actual != expected:
        raise _DeliveryError(
            "conflict", "Hevy's routine differs from the approved targets. Review it in the app."
        )
    if not all(isinstance(routine.get(key), str) and routine[key] for key in ("id", "updated_at")):
        raise _DeliveryError("unknown", "Hevy did not confirm a routine ID and version.")
    return {"status": "sent", "remote_id": routine["id"], "remote_version": routine["updated_at"]}


def _routine_limit(response: bytes) -> bool:
    try:
        value = json.loads(response)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("error") == "routine-limit-exceeded"


def _request(
    http: Callable,
    secrets: dict,
    method: str,
    path: str,
    value: dict | None = None,
    *,
    uncertain: bool = False,
) -> dict:
    write = method in ("POST", "PUT")
    uncertain = uncertain or write
    try:
        headers = {"api-key": secrets["api_key"], "accept": "application/json"}
        body = None if value is None else json.dumps(value, allow_nan=False).encode("utf-8")
        if body is not None:
            headers["content-type"] = "application/json"
        status, _, response = http(method, _API + path, headers, body)
        if status == 403 and _routine_limit(response):
            raise _DeliveryError(
                "needs_you", "Hevy's routine limit was reached. Review your routines in the app."
            )
        if status in (401, 403):
            raise _DeliveryError("needs_you", "Reconnect Hevy before sending.", auth_failed=True)
        if status == 429 and write:
            raise _DeliveryError("not_sent", "Hevy rejected the request due to its rate limit.")
        if status in (400, 404) and write:
            raise _DeliveryError(
                "needs_you", "Hevy refused the routine. Review the plan and app connection."
            )
        if status not in (200, 201):
            raise _DeliveryError(
                "unknown" if uncertain else "not_sent", "Hevy could not confirm this request."
            )
        return json.loads(response)
    except (OSError, HTTPException, ValueError) as error:
        raise _DeliveryError(
            "unknown" if uncertain else "not_sent", "Hevy could not confirm this request."
        ) from error


def _pages(request: Callable, name: str, size: int) -> list:
    first = request("GET", f"/{name}?page=1&pageSize={size}")
    count = first["page_count"]
    if type(count) is not int or count < 0:
        raise _DeliveryError("unknown", "Hevy returned an invalid page count.")
    result = list(first[name])
    for page in range(2, count + 1):
        result.extend(request("GET", f"/{name}?page={page}&pageSize={size}")[name])
    return result


def _readback(request: Callable, reply: dict, expected: dict, remote_id: str | None) -> dict:
    acknowledged = reply["routine"]["id"]
    if (
        not isinstance(acknowledged, str)
        or not acknowledged
        or (remote_id and acknowledged != remote_id)
    ):
        raise _DeliveryError("unknown", "Hevy did not confirm the expected routine ID.")
    stored = request("GET", "/routines/" + quote(acknowledged, safe=""), uncertain=True)["routine"]
    if stored["id"] != acknowledged:
        raise _DeliveryError("unknown", "Hevy returned an unexpected routine ID.")
    return _confirmed(stored, expected)


def _create(request: Callable, expected: dict, marker: str, in_doubt: bool) -> dict:
    matches = [
        routine for routine in _pages(request, "routines", 10) if routine["title"].endswith(marker)
    ]
    if len(matches) > 1:
        raise _DeliveryError(
            "needs_you", "Several Hevy routines have this operation marker. Review them in the app."
        )
    if matches:
        return _confirmed(matches[0], expected)
    if in_doubt:
        raise _DeliveryError(
            "unknown", "The prior Hevy creation is unconfirmed. Check the app before trying again."
        )
    reply = request("POST", "/routines", {"routine": expected})
    return _readback(request, reply, expected, None)


def _update(
    request: Callable,
    expected: dict,
    marker: str,
    remote_id: str,
    remote_version: str | None,
    in_doubt: bool,
) -> dict:
    path = "/routines/" + quote(remote_id, safe="")
    current = request("GET", path)["routine"]
    if current["id"] != remote_id:
        raise _DeliveryError("unknown", "Hevy returned an unexpected routine ID.")
    expected["folder_id"] = current.get("folder_id")
    if "notes" in current:
        expected["notes"] = current["notes"]
    if current["title"].endswith(marker):
        return _confirmed(current, expected)
    if in_doubt:
        raise _DeliveryError(
            "unknown",
            "The prior Hevy update is unconfirmed. Check the routine before trying again.",
        )
    if current.get("updated_at") != remote_version:
        raise _DeliveryError("conflict", "The Hevy routine changed since TFS last saw it.")
    reply = request("PUT", path, {"routine": expected})
    return _readback(request, reply, expected, remote_id)


def send(
    item: dict,
    op_id: str,
    remote_id: str | None,
    remote_version: str | None,
    in_doubt: bool,
    secrets: dict,
    now: str,
    http: Callable,
) -> dict:
    """Map approved strength targets and reconcile uncertain sends without repeating writes."""
    if not _WRITES_PROVEN:
        return {
            "status": "needs_you",
            "message": "Hevy routine delivery awaits provider verification.",
        }
    request = partial(_request, http, secrets, uncertain=in_doubt)
    try:
        if item["family"] != "strength" or item["status"] not in ("saved", "sent"):
            raise _DeliveryError(
                "needs_you", "This operation cannot be delivered as a Hevy routine."
            )
        marker = f"[TFS:{op_id}]"
        expected = {
            "title": f"{item['local_date']} {item['label']} {marker}",
            "folder_id": None,
            "exercises": _exercises(item["parts"], _pages(request, "exercise_templates", 100)),
        }
        if remote_id:
            return _update(request, expected, marker, remote_id, remote_version, in_doubt)
        return _create(request, expected, marker, in_doubt)
    except _DeliveryError as error:
        return error.result
    except (KeyError, TypeError, ValueError):
        return {"status": "unknown", "message": "Hevy returned an invalid routine response."}
