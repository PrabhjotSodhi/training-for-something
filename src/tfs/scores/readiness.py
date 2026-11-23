"""Readiness: HRV, resting heart rate and sleep against the user's own normal (metrics spec, HLD 8)."""

import json
import sqlite3
import tomllib
from datetime import date, timedelta
from importlib import resources
from statistics import median

SCORE = "readiness"
_MAD_TO_SIGMA = 1.4826
_COEFFICIENTS = tomllib.loads(
    resources.files("tfs.scores").joinpath("coefficients.toml").read_text("utf-8")
)
FORMULA_VERSION = _COEFFICIENTS["formula_version"]


def normal_band(
    source: str, history: dict[date, tuple[float, str]], day: date
) -> tuple[float, float, int] | str:
    """Return the median, the spread and the days used from the same source's previous days, or the reason there is no band."""
    window = [
        day - timedelta(days=offset) for offset in range(1, _COEFFICIENTS["baseline_days"] + 1)
    ]
    baseline = [history[past][0] for past in window if history.get(past, (0, None))[1] == source]
    needed = _COEFFICIENTS["min_baseline_days"]
    if len(baseline) < needed:
        return f"needs {needed} days, has {len(baseline)}"
    normal = median(baseline)
    spread = _MAD_TO_SIGMA * median(abs(other - normal) for other in baseline)
    if spread == 0:
        return "baseline never varies"
    return normal, spread, len(baseline)


def _robust_z(
    today: tuple[float, str], history: dict[date, tuple[float, str]], day: date
) -> tuple[float | None, dict]:
    """Return today's z score against the same source's previous days, or None with the reason."""
    value, source = today
    band = normal_band(source, history, day)
    if isinstance(band, str):
        return None, {"reason": band}
    normal, spread, days = band
    z_score = (value - normal) / spread
    return z_score, {"z": round(z_score, 2), "baseline_days": days, "source": source}


def _score_day(
    day: date, facts: dict[str, dict[date, tuple[float, str]]]
) -> tuple[int | None, int | None, int | None, dict]:
    inputs: dict[str, dict] = {}
    weighted = total_weight = 0.0
    for metric, setting in _COEFFICIENTS["inputs"].items():
        today = facts[metric].get(day)
        z_score, detail = (
            (None, {"reason": "no value today"})
            if today is None
            else _robust_z(today, facts[metric], day)
        )
        inputs[metric] = detail
        if z_score is not None:
            weighted += setting["weight"] * setting["direction"] * z_score
            total_weight += setting["weight"]
    used = sum("z" in detail for detail in inputs.values())
    if used < 2:
        return None, None, None, inputs
    low_clamp, high_clamp = _COEFFICIENTS["low_clamp"], _COEFFICIENTS["high_clamp"]

    def clamp(number: float) -> int:
        return round(min(high_clamp, max(low_clamp, number)))

    value = 50 + _COEFFICIENTS["scale"] * weighted / total_weight
    width = _COEFFICIENTS["range_width"]
    if used == 2:
        width *= 1 + _COEFFICIENTS["range_widening"]
    return clamp(value), clamp(value - width), clamp(value + width), inputs


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute every readiness row from the day facts and return how many rows that wrote.

    A day with no value keeps a row that names what is missing. The caller owns the transaction.
    """
    metrics = list(_COEFFICIENTS["inputs"])
    facts: dict[str, dict[date, tuple[float, str]]] = {metric: {} for metric in metrics}
    for local_date, metric, value, source in conn.execute(
        "SELECT local_date, metric, value, source FROM day_facts"
        " WHERE metric IN (SELECT value FROM json_each(?))",
        (json.dumps(metrics),),
    ):
        facts[metric][date.fromisoformat(local_date)] = (value, source)
    days = sorted({day for by_day in facts.values() for day in by_day})
    conn.execute("DELETE FROM scores WHERE score = ?", (SCORE,))
    for day in days:
        value, low, high, inputs = _score_day(day, facts)
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day.isoformat(), SCORE, value, low, high, json.dumps(inputs), FORMULA_VERSION),
        )
    return len(days)
