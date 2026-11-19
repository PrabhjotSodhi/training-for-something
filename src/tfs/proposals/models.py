"""The shape of every proposal action, written once (docs/specs/proposal-actions.md)."""

from datetime import date
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from tfs.connectors.core.contract import FAMILIES
from tfs.proposals.targets import GOAL_TYPES


def _iso_day(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("Use a YYYY-MM-DD date.")
    return value


def _http_url(value: str) -> str:
    link = urlsplit(value)
    if link.scheme not in ("https", "http") or not link.netloc:
        raise ValueError("Each source needs an HTTP or HTTPS URL.")
    return value


Text = Annotated[str, Field(pattern=r"\S")]
# Bounds on a union publish as raw ge and le keys, so each alias states its schema.
Number = Annotated[
    int | float,
    Field(ge=0, le=1e308),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 1e308}),
]
Minutes = Annotated[
    int | float,
    Field(gt=0, le=1e308),
    WithJsonSchema({"type": "number", "exclusiveMinimum": 0, "maximum": 1e308}),
]
Day = Annotated[str, AfterValidator(_iso_day), WithJsonSchema({"type": "string", "format": "date"})]
StartHint = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")] | None


class _Model(BaseModel):
    """Strict, so true is not 1 and "8" is not 8. A field that defaults to None may be left out."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Part(_Model):
    """One planned set, lap or interval, in seconds, metres, kg and beats per minute."""

    part_kind: Text
    label: Text = None
    duration_s: Number = None
    distance_m: Number = None
    reps: int = Field(None, ge=0, le=2**63 - 1)
    weight_kg: Number = None
    avg_hr: Number = None


class _Action(_Model):
    item: Text


class AddSession(_Action):
    """Plan a new session."""

    action: Literal["add_session"]
    date: Day
    family: Literal[*FAMILIES]
    label: Text
    parts: list[Part]
    goal_ids: list[Text] = None
    start_hint: StartHint = None


class MoveSession(_Action):
    """Move a planned session to another day."""

    action: Literal["move_session"]
    to: Day
    start_hint: StartHint = None


class ChangeSession(_Action):
    """Change the label, parts or start time of a planned session."""

    action: Literal["change_session"]
    label: Text = None
    parts: list[Part] = None
    start_hint: StartHint = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.model_fields_set <= {"action", "item"}:
            raise ValueError("A change needs a label, parts or start_hint.")
        return self


class RemoveSession(_Action):
    """Remove a planned session."""

    action: Literal["remove_session"]


class RestoreSession(_Action):
    """Bring a removed session back on an explicit day."""

    action: Literal["restore_session"]
    to: Day
    start_hint: StartHint = None


class MarkDone(_Action):
    """Mark a planned session done by hand."""

    action: Literal["mark_done"]


class UnlinkPlanMatch(_Action):
    """Stop an imported workout from completing a planned session."""

    action: Literal["unlink_plan_match"]
    record_ids: list[Text] = Field(min_length=1)


class GoalDescription(_Model):
    """A goal in words."""

    description: Text


class MeasuredGoal(GoalDescription):
    """A goal in words with a number and its unit."""

    value: Number
    unit: Text


class SetGoal(_Action):
    """Record a goal with a future target date."""

    action: Literal["set_goal"]
    type: Literal[*GOAL_TYPES]
    date: Day
    target: MeasuredGoal | GoalDescription


class Injury(_Model):
    """An injury in the user's own words."""

    description: Text


class UnavailableDay(_Model):
    """A day with no training."""

    date: Day


class MaxSessionLength(_Model):
    """The longest session the user accepts."""

    minutes: Minutes


class _SetLimit(_Action):
    action: Literal["set_limit"]


class SetInjuryLimit(_SetLimit):
    """Record an injury as a training limit."""

    kind: Literal["injury"]
    values: Injury


class SetUnavailableDayLimit(_SetLimit):
    """Record a day with no training."""

    kind: Literal["unavailable_day"]
    values: UnavailableDay


class SetMaxSessionLengthLimit(_SetLimit):
    """Record the longest session the user accepts."""

    kind: Literal["max_session_length"]
    values: MaxSessionLength


SetLimit = Annotated[
    SetInjuryLimit | SetUnavailableDayLimit | SetMaxSessionLengthLimit, Field(discriminator="kind")
]
Action = Annotated[
    AddSession
    | MoveSession
    | ChangeSession
    | RemoveSession
    | RestoreSession
    | MarkDone
    | UnlinkPlanMatch
    | SetGoal
    | SetLimit,
    Field(discriminator="action"),
]


class Source(_Model):
    """A page the proposal's reasoning rests on."""

    url: Annotated[str, AfterValidator(_http_url)]
    type: Text


class Proposal(_Model):
    """A batch of actions against one plan version, with the reason and its sources."""

    plan_version: int = Field(ge=0)
    why: Text
    sources: list[Source]
    actions: list[Action] = Field(min_length=1)
    delivery_targets: dict[Text, list[Text]] = None
