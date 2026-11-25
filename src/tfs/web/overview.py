"""The Overview layout to draw, read only from the coach_v1 views."""

import json
from contextlib import closing
from datetime import date, datetime, time, timedelta
from math import ceil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from tfs import providers
from tfs.connectors.core.contract import load_manifests
from tfs.diagnostics import is_stale
from tfs.mcp.tool_access import open_reader, query
from tfs.proposals.models import Plate, SetsByMuscleCard, SleepCard, TrendCard, WeekCard
from tfs.scores.readiness import normal_band
from tfs.web.catalogue import (
    DEFAULT_CARDS,
    JOIN_DAYS,
    LOAD_BAND,
    LOAD_PARTS,
    LOAD_SCORES,
    PLATE_ROWS,
    SPORT_UNITS,
    TREND_TITLES,
)

WINDOW_DAYS = 28
TREND_DAYS = 30
LOAD_COVERAGE_DAYS = 35


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
    """One tile: a done, planned or missed session in its sport's unit."""

    name: str
    sport: str
    minutes: float | None
    amount: float
    unit: str
    state: Literal["done", "planned", "missed"]


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
    """Monday to Sunday with the totals per sport and the number of plan items."""

    today: str
    days: list[WeekDay]
    totals: list[WeekTotal]
    planned_sessions: int


class WeekOverviewCard(WeekCard):
    """The week card with its numbers."""

    data: WeekData | None = None


class SleepNight(BaseModel):
    """One night: hours asleep on the day the person woke up, or null with no data.

    stages holds the hours of each reported stage (awake, rem, core, deep) and is empty
    when the source sent none.
    """

    date: str
    hours: float | None
    stages: dict[str, float]


class SleepData(BaseModel):
    """The last 7 nights, oldest first, with the saved sleep goal."""

    nights: list[SleepNight]
    target_h: float | None


class SleepOverviewCard(SleepCard):
    """The sleep card with its numbers."""

    data: SleepData | None = None


class TrendPoint(BaseModel):
    """One day: the value with its normal band, or nulls with no reading or no band."""

    date: str
    value: float | None
    low: float | None
    high: float | None


class TrendPart(BaseModel):
    """One line on a trend card. Training load has one per part and a percent, never load points."""

    key: str
    name: str
    value: float | None
    state: Literal["Below normal", "Normal", "Above normal"] | None
    percent: int | None
    points: list[TrendPoint]


class TrendData(BaseModel):
    """The last 30 days ending today. Readings up to join_days apart are joined by a line."""

    title: str
    unit: str | None
    join_days: int
    selected: str
    parts: list[TrendPart]


class TrendOverviewCard(TrendCard):
    """A trend card with its numbers."""

    data: TrendData | None = None


class MuscleSets(BaseModel):
    """One muscle and its working sets this week."""

    name: str
    sets: int


class SetsByMuscleData(BaseModel):
    """This week's muscles, most sets first. Empty when the week has no sets."""

    muscles: list[MuscleSets]


class SetsByMuscleOverviewCard(SetsByMuscleCard):
    """The sets by muscle card with its numbers."""

    data: SetsByMuscleData | None = None


class FirstRunWorkout(BaseModel):
    """The latest workout, with the app it came from when known."""

    date: str
    type: str
    minutes: float | None
    source: str | None


class WaitingSource(BaseModel):
    """A turned on app with no records yet. A phone app also needs the TFS window open."""

    app: str
    phone: bool


class FirstRun(BaseModel):
    """What the Overview shows while no goal is set."""

    workout: FirstRunWorkout | None
    waiting: list[WaitingSource]
    message: str


class Overview(BaseModel):
    """The plate and the cards to draw."""

    plate: PlateOverview | None
    first_run: FirstRun | None
    cards: list[
        Annotated[
            WeekOverviewCard | SleepOverviewCard | TrendOverviewCard | SetsByMuscleOverviewCard,
            Field(discriminator="type"),
        ]
    ]
    new_version: str | None = None


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
    if card["type"] == "sets_by_muscle":
        return "sets_by_muscle" in have
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


def _week_data(connection, now: datetime) -> WeekData:
    today = now.date()
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
    stale_after = {
        name: manifest.get("stale_after_hours") for name, manifest in load_manifests().items()
    }
    sources = _rows(
        connection, "SELECT connector, last_contact_at FROM coach_v1_sources WHERE enabled = 1"
    )
    known = bool(sources) and not any(
        is_stale(contact, stale_after.get(name), now.isoformat()) for name, contact in sources
    )
    unsorted = {day for day, family, *_ in workouts.values() if family is None}
    sessions: dict[str, list[WeekSession]] = {day: [] for day in days}
    totals: dict[tuple[str, str], list[float]] = {}
    planned_sessions = 0

    def missed(day: str) -> bool:
        cutoff = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(12), now.tzinfo)
        return known and day not in unsorted and now >= cutoff

    def add(day: str, name: str, sport: str, minutes, done, planned) -> None:
        amount, unit = done or planned
        sessions[day].append(
            WeekSession(
                name=name,
                sport=sport,
                minutes=minutes,
                amount=amount,
                unit=unit,
                state="done" if done else "missed" if missed(day) else "planned",
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
        planned_sessions += 1
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
        planned_sessions=planned_sessions,
    )


def _sleep_data(connection, target_h: float | None, today: date) -> SleepData:
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    asleep = {}
    stages = {day: {} for day in days}
    for day, metric, hours in _rows(
        connection,
        "SELECT local_date, metric, value FROM coach_v1_day_facts"
        " WHERE metric IN ('sleep_asleep', 'sleep_awake', 'sleep_rem', 'sleep_core', 'sleep_deep')"
        " ORDER BY local_date DESC",
    ):
        if day not in stages:
            continue
        if metric == "sleep_asleep":
            asleep[day] = hours
        else:
            stages[day][metric.removeprefix("sleep_")] = hours
    return SleepData(
        nights=[SleepNight(date=day, hours=asleep.get(day), stages=stages[day]) for day in days],
        target_h=target_h,
    )


def _sets_by_muscle_data(connection, today: date) -> SetsByMuscleData:
    monday = (today - timedelta(days=today.weekday())).isoformat()
    rows = _rows(
        connection,
        "SELECT week_start, muscle, sets FROM coach_v1_sets_by_muscle ORDER BY sets DESC, muscle",
    )
    return SetsByMuscleData(
        muscles=[
            MuscleSets(name=muscle, sets=sets) for week, muscle, sets in rows if week == monday
        ]
    )


def _first_run(connection) -> FirstRun:
    manifests = load_manifests()
    labels = {name: manifest["label"] for name, manifest in manifests.items()}
    latest = _rows(
        connection,
        "SELECT local_date, family, duration_min, source FROM coach_v1_workouts"
        " ORDER BY start_utc DESC LIMIT 1",
    )
    sources = _rows(
        connection,
        "SELECT connector FROM coach_v1_sources WHERE enabled = 1 AND covered_until IS NULL"
        " ORDER BY connector",
    )
    workout = None
    if latest:
        day, family, minutes, source = latest[0]
        workout = FirstRunWorkout(
            date=day,
            type=family or "other",
            minutes=minutes,
            source=source and labels.get(source, source),
        )
    return FirstRun(
        workout=workout,
        waiting=[
            WaitingSource(
                app=labels.get(name, name),
                phone=manifests.get(name, {}).get("routes", {}).get("push", False),
            )
            for (name,) in sources
        ],
        message=providers.FIRST_MESSAGE,
    )


def _state(value: float, low: float, high: float) -> str:
    return "Above normal" if value > high else "Below normal" if value < low else "Normal"


def _trend_days(today: date) -> list[date]:
    return [today - timedelta(days=offset) for offset in range(TREND_DAYS - 1, -1, -1)]


def _metric_part(connection, metric: str, today: date) -> tuple[TrendPart, str] | None:
    rows = [
        row
        for row in _rows(
            connection,
            "SELECT local_date, metric, value, unit, source FROM coach_v1_day_facts"
            " WHERE metric IN ('resting_hr', 'hrv_sdnn', 'weight', 'vo2_max')",
        )
        if row[1] == metric
    ]
    history = {date.fromisoformat(day): (value, source) for day, _, value, _, source in rows}
    units = {date.fromisoformat(row[0]): row[3] for row in rows}
    points = []
    for day in _trend_days(today):
        if day not in history:
            points.append(TrendPoint(date=day.isoformat(), value=None, low=None, high=None))
            continue
        value, source = history[day]
        band = normal_band(source, history, day)
        low, high = (
            (None, None) if isinstance(band, str) else (band[0] - band[1], band[0] + band[1])
        )
        points.append(
            TrendPoint(
                date=day.isoformat(),
                value=round(value, 1),
                low=None if low is None else round(low, 1),
                high=None if high is None else round(high, 1),
            )
        )
    latest = next((point for point in reversed(points) if point.value is not None), None)
    if latest is None:
        return None
    state = None if latest.low is None else _state(latest.value, latest.low, latest.high)
    part = TrendPart(
        key=metric,
        name=TREND_TITLES[metric],
        value=latest.value,
        state=state,
        percent=None,
        points=points,
    )
    return part, units[date.fromisoformat(latest.date)]


def _load_ratio(daily: dict[date, float], workouts: list, day: date) -> float | None:
    """Return the last 7 days of load over the usual week, or None when the part does not show."""
    if min(daily) > day - timedelta(days=28):
        return None
    usual = sum(daily.get(day - timedelta(days=back), 0) for back in range(7, 35)) / 4
    recent = sum(daily.get(day - timedelta(days=back), 0) for back in range(7))
    window = [
        covered
        for found, covered in workouts
        if day - timedelta(days=LOAD_COVERAGE_DAYS - 1) <= found <= day
    ]
    if 4 * sum(window) < 3 * len(window):
        return None
    return recent / usual if usual > 0 else None


def _load_parts(connection, today: date) -> list[TrendPart]:
    scores = _rows(
        connection,
        "SELECT local_date, score, value FROM coach_v1_scores"
        " WHERE score IN ('load_cardio', 'load_strength', 'load_effort')",
    )
    workouts = [
        (date.fromisoformat(day), bool(minutes and avg_hr))
        for day, minutes, avg_hr in _rows(
            connection, "SELECT local_date, duration_min, avg_hr FROM coach_v1_workouts"
        )
    ]
    parts = []
    for key, (name, score) in LOAD_PARTS.items():
        daily = {date.fromisoformat(day): value for day, found, value in scores if found == score}
        if not daily:
            continue
        points = []
        for day in _trend_days(today):
            found = _load_ratio(daily, workouts if key == "cardio" else [], day)
            points.append(
                TrendPoint(
                    date=day.isoformat(),
                    value=None if found is None else round(found, 3),
                    low=None if found is None else LOAD_BAND[0],
                    high=None if found is None else LOAD_BAND[1],
                )
            )
        if points[-1].value is not None:
            parts.append(
                TrendPart(
                    key=key,
                    name=name,
                    value=None,
                    state=_state(points[-1].value, *LOAD_BAND),
                    percent=round((points[-1].value - 1) * 100),
                    points=points,
                )
            )
    return parts


def _trend_data(connection, card: dict, today: date) -> TrendData | None:
    metric = card["metric"]
    unit = None
    if metric == "training_load":
        parts = _load_parts(connection, today)
    else:
        found = _metric_part(connection, metric, today)
        parts, unit = ([found[0]], found[1]) if found else ([], None)
    if not parts:
        return None
    keys = [part.key for part in parts]
    return TrendData(
        title=TREND_TITLES[metric],
        unit=unit,
        join_days=JOIN_DAYS.get(metric, 1),
        selected=card.get("first") if card.get("first") in keys else keys[0],
        parts=parts,
    )


def _card_data(
    connection, card: dict, now: datetime
) -> WeekData | SleepData | TrendData | SetsByMuscleData | None:
    today = now.date()
    if card["type"] == "week":
        return _week_data(connection, now)
    if card["type"] == "sets_by_muscle":
        return _sets_by_muscle_data(connection, today)
    if card["type"] == "sleep":
        return _sleep_data(connection, card.get("target_h"), today)
    return _trend_data(connection, card, today)


def _cards(connection, cards: list[dict], now: datetime) -> list[dict]:
    drawn = [{**card, "data": _card_data(connection, card, now)} for card in cards]
    return [card for card in drawn if card["data"] is not None]


def read_overview(db_path: Path, now: datetime) -> Overview:
    """Return the saved layout, or the default one built from the last 28 days of data.

    now is the local time, so today and the end of each day follow the person's clock.
    """
    today = now.date()
    since = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    with closing(open_reader(db_path)) as connection:
        saved = _rows(connection, "SELECT layout_json FROM coach_v1_overview")
        has_goal = bool(_rows(connection, "SELECT 1 FROM coach_v1_goals LIMIT 1"))
        first_run = None if has_goal else _first_run(connection)
        if saved:
            layout = json.loads(saved[0][0])
            plate = {**layout["plate"], "data": _plate_data(connection, layout["plate"], today)}
            return Overview.model_validate(
                {
                    "plate": plate,
                    "first_run": first_run,
                    "cards": _cards(connection, layout["cards"], now),
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
        if any(
            week >= since
            for (week,) in _rows(connection, "SELECT week_start FROM coach_v1_sets_by_muscle")
        ):
            have.add("sets_by_muscle")
        goal = next((goal_id for goal_id, day in goals if day >= today.isoformat()), None)
        plate = None
        if goal:
            plate = {
                "goal_id": goal,
                "colour": "orange",
                "rows": [r for r in PLATE_ROWS if r in have],
            }
            plate["data"] = _plate_data(connection, plate, today)
        cards = _cards(connection, [card for card in DEFAULT_CARDS if _has_data(card, have)], now)
    return Overview.model_validate({"plate": plate, "first_run": first_run, "cards": cards})
