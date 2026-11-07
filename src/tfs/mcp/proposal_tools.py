"""Schemas and dispatch for local proposal tools; the client owns approval."""

from datetime import UTC, datetime
from pathlib import Path

from tfs.connectors.core.contract import FAMILIES
from tfs.proposals import apply
from tfs.proposals.targets import GOAL_TYPES

TEXT = {"type": "string", "minLength": 1}
NUMBER = {"type": "number", "minimum": 0}
DAY = {"type": "string", "format": "date"}


def _object(required: dict, optional: dict | None = None) -> dict:
    return {
        "type": "object",
        "properties": required | (optional or {}),
        "required": list(required),
        "additionalProperties": False,
    }


def _action(name: str, required: dict, optional: dict | None = None) -> dict:
    return _object({"action": {"const": name}, "item": TEXT, **required}, optional)


PART = _object(
    {"part_kind": TEXT},
    {
        "label": TEXT,
        "duration_s": NUMBER,
        "distance_m": NUMBER,
        "reps": {"type": "integer", "minimum": 0},
        "weight_kg": NUMBER,
        "avg_hr": NUMBER,
    },
)
PARTS = {"type": "array", "items": PART}
ACTIONS = [
    _action(
        "add_session",
        {"date": DAY, "family": {"enum": list(FAMILIES)}, "label": TEXT, "parts": PARTS},
        {"goal_ids": {"type": "array", "items": TEXT}},
    ),
    _action("move_session", {"to": DAY}),
    {
        **_action("change_session", {}, {"label": TEXT, "parts": PARTS}),
        "anyOf": [{"required": ["label"]}, {"required": ["parts"]}],
    },
    _action("remove_session", {}),
    _action("mark_done", {}),
    _action(
        "set_goal",
        {
            "type": {"enum": list(GOAL_TYPES)},
            "date": DAY,
            "target": {
                "oneOf": [
                    _object({"description": TEXT}),
                    _object({"description": TEXT, "value": NUMBER, "unit": TEXT}),
                ]
            },
        },
    ),
    *[
        _action("set_limit", {"kind": {"const": kind}, "values": _object(fields)})
        for kind, fields in [
            ("injury", {"description": TEXT}),
            ("unavailable_day", {"date": DAY}),
            ("max_session_length", {"minutes": {"type": "number", "exclusiveMinimum": 0}}),
        ]
    ],
]
PROPOSAL = _object(
    {
        "plan_version": {"type": "integer", "minimum": 0},
        "why": TEXT,
        "sources": {
            "type": "array",
            "items": _object({"url": {"type": "string", "format": "uri"}, "type": TEXT}),
        },
        "actions": {"type": "array", "minItems": 1, "items": {"oneOf": ACTIONS}},
    }
)


def _tool(name: str, description: str, required: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": _object(required),
        "annotations": {
            "readOnlyHint": name == "propose",
            "destructiveHint": name != "propose",
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        "propose",
        "Preview a local plan, goal or training-limit proposal without saving. Read coach_v1_plan_state first. Parts use seconds, metres, kg and beats/minute. Dates use the computer's local day. Goal dates must be future. Show the exact changes, reason and sources before requesting Save approval.",
        {"proposal": PROPOSAL},
    ),
    _tool(
        "save",
        "Save the exact reviewed proposal only through the AI client's user permission prompt. This changes local state and sends nothing to apps yet. Never treat an AI-generated approved flag as consent. Reuse request_id for retries of identical content; a changed proposal needs a new ID. Always-allow client permissions can skip the prompt.",
        {"proposal": PROPOSAL, "request_id": TEXT},
    ),
    _tool(
        "undo",
        "Undo the latest local change at target_version after user approval through the client's permission prompt. Read the current global version first. Later saves make this stale. Reuse request_id for identical retries. This does not reverse provider deliveries.",
        {"target_version": {"type": "integer", "minimum": 1}, "request_id": TEXT},
    ),
]


def run(name: str, arguments: dict, db_path: Path) -> dict:
    """Use the shared local APIs; approval is provided by the calling client."""
    now = datetime.now(UTC).astimezone()
    if name == "propose":
        return apply.preview(db_path, arguments["proposal"], now=now)
    options = {"request_id": arguments["request_id"], "approved_by": "chat", "now": now}
    if name == "save":
        return apply.save(db_path, arguments["proposal"], **options)
    return apply.undo(db_path, arguments["target_version"], **options)
