"""The Overview layout to draw, read only from the coach_v1 views."""

import json
from contextlib import closing
from datetime import date, timedelta
from math import ceil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from tfs.mcp.tool_access import open_reader, query
from tfs.proposals.models import Plate, SleepCard, TrendCard, WeekCard
from tfs.web.catalogue import DEFAULT_CARDS, LOAD_SCORES, PLATE_ROWS, SPORT_UNITS

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


class WeekSession(BaseModel):
    """One tile: a planned or done session in its sport's unit."""

    name: str
    sport: str
    minutes: float | None
    amount: float
    unit: str
    state: Literal["done", "planned"]


class WeekDay(BaseModel):
    """One day of the week with its sessions."""

    date: str
    sessions: list[WeekSession]


class WeekTotal(BaseModel):
    """What is done of what is planned for one sport."""

    sport: str
    unit: str
    done: float
    planned: float


class WeekData(BaseModel):
    """Monday to Sunday with the totals per sport."""

    today: str
    days: list[WeekDay]
    totals: list[WeekTotal]


class WeekOverviewCard(WeekCard):
    """The week card with its numbers."""

    data: WeekData | None = None


class SleepNight(BaseModel):
    """One night: hours asleep on the day the person woke up, or null with no data."""

    date: str
    hours: float | None


class SleepData(BaseModel):
    """The last 7 nights, oldest first, with the saved sleep goal."""

    nights: list[SleepNight]
    target_h: float | None


class SleepOverviewCard(SleepCard):
    """The sleep card with its numbers."""

    data: SleepData | None = None


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


def _amount(sport: str, km: float | None, sets: int, minutes: float | None) -> tuple[float, str]:
    unit = SPORT_UNITS.get(sport)
    value = {"km": km, "m": (km or 0) * 1000, "sets": sets}.get(unit)
    if value:
        return round(value, 2), unit
    return round(minutes or 0, 1), "min"


def _week_data(connection, today: date) -> WeekData:
    monday = today - timedelta(days=today.weekday())
    days = [(monday + timedelta(days=offset)).isoformat() for offset in range(7)]
    plan_sums = {
        row[0]: row[1:]
        for row in _rows(
            connection,
            "SELECT plan_item_id, sum(part_kind = 'set'), sum(distance_km), sum(duration_min)"
            " FROM coach_v1_plan_parts GROUP BY plan_item_id",
        )
    }
    set_counts = dict(
        _rows(
            connection,
            "SELECT workout_id, sum(part_kind = 'set') FROM coach_v1_workout_parts"
            " GROUP BY workout_id",
        )
    )
    workouts = {
        row[0]: row[1:]
        for row in _rows(
            connection,
            "SELECT workout_id, local_date, family, label, duration_min, distance_km"
            " FROM coach_v1_workouts",
        )
        if row[1] in days
    }
    sessions: dict[str, list[WeekSession]] = {day: [] for day in days}
    totals: dict[tuple[str, str], list[float]] = {}

    def add(day: str, name: str, sport: str, minutes, done, planned) -> None:
        amount, unit = done or planned
        sessions[day].append(
            WeekSession(
                name=name,
                sport=sport,
                minutes=minutes,
                amount=amount,
                unit=unit,
                state="done" if done else "planned",
            )
        )
        total = totals.setdefault((sport, unit), [0, 0])
        total[0] += amount if done else 0
        total[1] += planned[0] if planned and planned[1] == unit else 0

    def workout_amount(workout_id: str, sport: str) -> tuple[tuple[float, str], float | None]:
        minutes, km = workouts[workout_id][3:]
        return _amount(sport, km, set_counts.get(workout_id) or 0, minutes), minutes

    matched = set()
    for item_id, day, sport, label, status, workout_id in _rows(
        connection,
        "SELECT plan_item_id, local_date, family, label, status, matched_workout_id"
        " FROM coach_v1_plan_items ORDER BY local_date, plan_item_id",
    ):
        if day not in days or status in ("removed", "replaced", "skipped"):
            continue
        sets, km, minutes = plan_sums.get(item_id, (0, None, None))
        planned = _amount(sport, km, sets or 0, minutes)
        if workout_id in workouts:
            matched.add(workout_id)
            done, minutes = workout_amount(workout_id, sport)
            add(day, label, sport, minutes, done, planned)
        else:
            add(day, label, sport, minutes, planned if status == "done" else None, planned)
    for workout_id, (day, family, label, *_) in workouts.items():
        if workout_id not in matched:
            sport = family or "other"
            done, minutes = workout_amount(workout_id, sport)
            add(day, label or sport, sport, minutes, done, None)
    return WeekData(
        today=today.isoformat(),
        days=[WeekDay(date=day, sessions=sessions[day]) for day in days],
        totals=[
            WeekTotal(sport=sport, unit=unit, done=round(done, 2), planned=round(planned, 2))
            for (sport, unit), (done, planned) in sorted(totals.items())
        ],
    )


def _sleep_data(connection, target_h: float | None, today: date) -> SleepData:
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    asleep = {
        day: hours
        for day, hours in _rows(
            connection,
            "SELECT local_date, value FROM coach_v1_day_facts WHERE metric = 'sleep_asleep'",
        )
        if day in days
    }
    return SleepData(
        nights=[SleepNight(date=day, hours=asleep.get(day)) for day in days], target_h=target_h
    )


def _card_data(connection, card: dict, today: date) -> WeekData | SleepData | None:
    if card["type"] == "week":
        return _week_data(connection, today)
    if card["type"] == "sleep":
        return _sleep_data(connection, card.get("target_h"), today)
    return None


def _cards(connection, cards: list[dict], today: date) -> list[dict]:
    return [{**card, "data": _card_data(connection, card, today)} for card in cards]


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
                    "cards": _cards(connection, layout["cards"], today),
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
        cards = _cards(connection, [card for card in DEFAULT_CARDS if _has_data(card, have)], today)
    return Overview.model_validate({"plate": plate, "cards": cards})
