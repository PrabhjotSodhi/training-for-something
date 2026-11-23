"""The Overview layout to draw, read only from the coach_v1 views."""

import json
from contextlib import closing
from datetime import date, timedelta
from math import ceil
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, Field

from tfs.mcp.tool_access import open_reader, query
from tfs.proposals.models import Plate, SleepCard, TrendCard, WeekCard
from tfs.web.catalogue import DEFAULT_CARDS, LOAD_SCORES, PLATE_ROWS

WINDOW_DAYS = 28


class PlateForecast(BaseModel):
    """A forecast range as text, placed on a 0 to 100 scale with the target if there is one."""

    label: str
    low: str
    high: str
    unit: str | None
    start: float
    end: float
    target: float | None
    target_text: str | None


class PlateRow(BaseModel):
    """One Today row: the row name and its value for today."""

    name: str
    value: str


class PlateData(BaseModel):
    """What the plate shows."""

    goal: str
    date: str
    days_to_go: int
    week: int | None
    weeks: int | None
    forecast: PlateForecast | None
    today: list[PlateRow]


class PlateOverview(Plate):
    """The plate with its numbers."""

    data: PlateData | None = None


class WeekOverviewCard(WeekCard):
    """The week card with its numbers."""

    data: None = None


class SleepOverviewCard(SleepCard):
    """The sleep card with its numbers."""

    data: None = None


class TrendOverviewCard(TrendCard):
    """A trend card with its numbers."""

    data: None = None


class Overview(BaseModel):
    """The plate and the cards to draw."""

    plate: PlateOverview | None
    cards: list[
        Annotated[
            WeekOverviewCard | SleepOverviewCard | TrendOverviewCard, Field(discriminator="type")
        ]
    ]


def _rows(connection, sql: str) -> list[list]:
    result = query(connection, sql)
    if "error" in result:
        raise HTTPException(500, "The overview could not be read. See the TFS log.")
    return result["rows"]


def _has_data(card: dict, have: set[str]) -> bool:
    if card["type"] == "week":
        return True
    if card["type"] == "sleep":
        return "sleep_asleep" in have
    if card["metric"] == "training_load":
        return bool(have & set(LOAD_SCORES))
    return card["metric"] in have


def _text(value: float, unit: str) -> str:
    if unit == "minutes":
        minutes = round(value)
        return f"{minutes // 60}:{minutes % 60:02d}"
    return f"{value:g}"


def _plate_data(connection, plate: dict, today: date) -> PlateData | None:
    goal_id = plate["goal_id"]
    goals = {
        row[0]: row[1:]
        for row in _rows(
            connection, "SELECT goal_id, type, local_date, target_json FROM coach_v1_goals"
        )
    }
    if goal_id not in goals:
        return None
    kind, goal_date, target_json = goals[goal_id]
    forecast_row = {
        row[0]: row[1:]
        for row in _rows(
            connection, "SELECT goal_id, low, high, unit, label FROM coach_v1_forecasts"
        )
    }.get(goal_id)
    plan_start = min(
        (
            start
            for ids, start in _rows(
                connection,
                "SELECT goal_ids_json, MIN(local_date) FROM coach_v1_plan_items"
                " GROUP BY goal_ids_json",
            )
            if goal_id in json.loads(ids)
        ),
        default=None,
    )
    todays = {
        name: f"{round(value, 1):g} {unit}".strip()
        for day, name, value, unit in _rows(
            connection,
            "SELECT local_date, metric, value, unit FROM coach_v1_day_facts"
            " UNION ALL SELECT local_date, score, value, '' FROM coach_v1_scores"
            " WHERE value IS NOT NULL ORDER BY 1 DESC LIMIT 100",
        )
        if day == today.isoformat()
    }
    days_to_go = max(0, (date.fromisoformat(goal_date) - today).days)
    week = weeks = None
    if plan_start:
        start = date.fromisoformat(plan_start)
        weeks = max(1, ceil((date.fromisoformat(goal_date) - start).days / 7))
        week = min(weeks, max(1, (today - start).days // 7 + 1))
    forecast = None
    if forecast_row:
        low, high, unit, label = forecast_row
        target = json.loads(target_json)
        target_value = target["value"] if target.get("unit") == unit else None
        bottom = min(low, target_value if target_value is not None else low)
        top = max(high, target_value if target_value is not None else high)
        pad = (high - low) / 2
        bottom, top = bottom - pad, top + pad
        span = (top - bottom) or 1

        def place(value: float) -> float:
            return (value - bottom) / span * 100

        forecast = PlateForecast(
            label=label,
            low=_text(low, unit),
            high=_text(high, unit),
            unit=None if unit == "minutes" else unit,
            start=place(low),
            end=place(high),
            target=None if target_value is None else place(target_value),
            target_text=None if target_value is None else _text(target_value, unit),
        )
    return PlateData(
        goal=kind,
        date=goal_date,
        days_to_go=days_to_go,
        week=week,
        weeks=weeks,
        forecast=forecast,
        today=[PlateRow(name=row, value=todays.get(row, "No data")) for row in plate["rows"]],
    )


def read_overview(db_path: Path, today: date) -> Overview:
    """Return the saved layout, or the default one built from the last 28 days of data."""
    since = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    with closing(open_reader(db_path)) as connection:
        saved = _rows(connection, "SELECT layout_json FROM coach_v1_overview")
        if saved:
            layout = json.loads(saved[0][0])
            plate = {**layout["plate"], "data": _plate_data(connection, layout["plate"], today)}
            return Overview.model_validate(
                {
                    "plate": plate,
                    "cards": [{**card, "data": None} for card in layout["cards"]],
                }
            )
        goals = _rows(
            connection, "SELECT goal_id, local_date FROM coach_v1_goals ORDER BY local_date"
        )
        latest = _rows(
            connection,
            "SELECT metric, MAX(local_date) FROM coach_v1_day_facts GROUP BY metric"
            " UNION ALL SELECT score, MAX(local_date) FROM coach_v1_scores"
            " WHERE value IS NOT NULL GROUP BY score",
        )
        have = {name for name, day in latest if day >= since}
        goal = next((goal_id for goal_id, day in goals if day >= today.isoformat()), None)
        plate = None
        if goal:
            plate = {
                "goal_id": goal,
                "colour": "orange",
                "rows": [r for r in PLATE_ROWS if r in have],
            }
            plate["data"] = _plate_data(connection, plate, today)
    cards = [{**card, "data": None} for card in DEFAULT_CARDS if _has_data(card, have)]
    return Overview.model_validate({"plate": plate, "cards": cards})
