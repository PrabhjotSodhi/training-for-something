"""The Overview layout to draw, read only from the coach_v1 views."""

import json
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, Field

from tfs.mcp.tool_access import open_reader, query
from tfs.proposals.models import Plate, SleepCard, TrendCard, WeekCard
from tfs.web.catalogue import DEFAULT_CARDS, LOAD_SCORES, PLATE_ROWS

WINDOW_DAYS = 28


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

    plate: Plate | None
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


def read_overview(db_path: Path, today: date) -> Overview:
    """Return the saved layout, or the default one built from the last 28 days of data."""
    since = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    with closing(open_reader(db_path)) as connection:
        saved = _rows(connection, "SELECT layout_json FROM coach_v1_overview")
        if saved:
            layout = json.loads(saved[0][0])
            return Overview.model_validate(
                {**layout, "cards": [{**card, "data": None} for card in layout["cards"]]}
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
    plate = (
        {"goal_id": goal, "colour": "orange", "rows": [r for r in PLATE_ROWS if r in have]}
        if goal
        else None
    )
    cards = [{**card, "data": None} for card in DEFAULT_CARDS if _has_data(card, have)]
    return Overview.model_validate({"plate": plate, "cards": cards})
