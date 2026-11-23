"""Training load per day, against the user's own normal: strength (reps x weight) and cardio (heart rate TRIMP). Metrics spec."""

import json
import sqlite3
import tomllib
from collections import defaultdict
from datetime import date, timedelta
from importlib import resources
from math import exp
from statistics import mean, median

SCORE = "load_strength"
CARDIO_SCORE = "load_cardio"
EFFORT_SCORE = "load_effort"
_ALL_COEFFICIENTS = tomllib.loads(
    resources.files("tfs.scores").joinpath("coefficients.toml").read_text("utf-8")
)
_COEFFICIENTS = _ALL_COEFFICIENTS["load_strength"]
_CARDIO = _ALL_COEFFICIENTS["load_cardio"]
_EFFORT = _ALL_COEFFICIENTS["load_effort"]
FORMULA_VERSION = _COEFFICIENTS["formula_version"]
CARDIO_FORMULA_VERSION = _CARDIO["formula_version"]
EFFORT_FORMULA_VERSION = _EFFORT["formula_version"]

# Each workout counts once: its parts come from the member with the most parts, as in coach_v1_workout_parts.
_WORKING_SETS = """
SELECT g.local_date, p.reps, p.weight_kg, json_extract(p.extras_json, '$.rpe')
FROM session_groups g
JOIN session_parts p ON p.record_id = (
  SELECT m.record_id FROM session_group_members m
  JOIN source_records live ON live.id = m.record_id AND live.deleted_at IS NULL
  WHERE m.group_id = g.id
  ORDER BY (SELECT count(*) FROM session_parts WHERE record_id = m.record_id) DESC, m.record_id
  LIMIT 1)
WHERE p.part_kind = 'set' AND coalesce(json_extract(p.extras_json, '$.set_type'), '') != 'warmup'
"""


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute every strength load row and return how many rows that wrote.

    The caller owns the transaction.
    """
    loads: dict[date, float] = defaultdict(float)
    sets: dict[date, list] = defaultdict(list)
    for local_date, reps, weight_kg, rpe in conn.execute(_WORKING_SETS):
        day = date.fromisoformat(local_date)
        loads[day] += (reps or 0) * (weight_kg or 0)
        sets[day].append((weight_kg, rpe))
    conn.execute("DELETE FROM scores WHERE score = ?", (SCORE,))
    for day in sorted(loads):
        previous = [
            loads[past]
            for offset in range(1, _COEFFICIENTS["baseline_days"] + 1)
            if (past := day - timedelta(days=offset)) in loads
        ]
        inputs: dict = {
            "working_sets": len(sets[day]),
            "normal": round(median(previous)) if previous else None,
            "normal_days": len(previous),
        }
        if rpes := [rpe for _, rpe in sets[day] if rpe is not None]:
            inputs["mean_rpe"] = round(mean(rpes), 1)
        if any(not weight_kg for weight_kg, _ in sets[day]):
            inputs["note"] = "body weight sets not counted"
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            (day.isoformat(), SCORE, round(loads[day]), json.dumps(inputs), FORMULA_VERSION),
        )
    return len(loads)


def rebuild_cardio(conn: sqlite3.Connection) -> int:
    """Recompute every cardio load row and return how many rows that wrote.

    Each workout group counts once, from its chosen duration and heart rates. Resting is the
    median resting heart rate of the previous days, not today. Max is the highest workout max
    heart rate up to and including today. A day without both has no row. The caller owns the
    transaction.
    """
    workouts: dict[date, list[tuple]] = defaultdict(list)
    for local_date, duration_s, avg_hr, max_hr in conn.execute(
        "SELECT local_date, json_extract(chosen_json, '$.duration_s.value'),"
        " json_extract(chosen_json, '$.avg_hr.value'), json_extract(chosen_json, '$.max_hr.value')"
        " FROM session_groups"
    ):
        workouts[date.fromisoformat(local_date)].append((duration_s, avg_hr, max_hr))
    resting = {
        date.fromisoformat(local_date): value
        for local_date, value in conn.execute(
            "SELECT local_date, value FROM day_facts WHERE metric = 'resting_hr'"
        )
    }
    max_by_day = {
        day: max((max_hr for _, _, max_hr in found if max_hr), default=None)
        for day, found in workouts.items()
    }
    loads: dict[date, float] = {}
    rows: dict[date, dict] = {}
    for day in sorted(workouts):
        with_heart_rate = [(d, a) for d, a, _ in workouts[day] if d and a]
        if not with_heart_rate:
            continue
        resting_values = [
            resting[past]
            for offset in range(1, _CARDIO["resting_days"] + 1)
            if (past := day - timedelta(days=offset)) in resting
        ]
        max_values = [
            found
            for offset in range(_CARDIO["max_days"])
            if (found := max_by_day.get(day - timedelta(days=offset)))
        ]
        if not resting_values or not max_values:
            continue
        resting_hr, max_hr = median(resting_values), max(max_values)
        if max_hr <= resting_hr:
            continue
        total = 0.0
        for duration_s, avg_hr in with_heart_rate:
            reserve = min(1, max(0, (avg_hr - resting_hr) / (max_hr - resting_hr)))
            total += (
                duration_s
                / 60
                * reserve
                * _CARDIO["trimp_factor"]
                * exp(_CARDIO["trimp_exponent"] * reserve)
            )
        loads[day] = total
        rows[day] = {
            "workouts": len(with_heart_rate),
            "resting_hr": resting_hr,
            "max_hr": max_hr,
        }
        if without := len(workouts[day]) - len(with_heart_rate):
            rows[day]["note"] = f"{without} workout{'s' * (without > 1)} without heart rate"
    conn.execute("DELETE FROM scores WHERE score = ?", (CARDIO_SCORE,))
    for day in sorted(loads):
        previous = [
            loads[past]
            for offset in range(1, _CARDIO["baseline_days"] + 1)
            if (past := day - timedelta(days=offset)) in loads
        ]
        rows[day] |= {
            "normal": round(median(previous)) if previous else None,
            "normal_days": len(previous),
        }
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            (
                day.isoformat(),
                CARDIO_SCORE,
                round(loads[day]),
                json.dumps(rows[day]),
                CARDIO_FORMULA_VERSION,
            ),
        )
    return len(loads)


def rebuild_effort(conn: sqlite3.Connection) -> int:
    """Recompute every effort load row and return how many rows that wrote.

    Each workout group counts once: its effort (the person's rating, else the estimate) x its
    duration in minutes. A day with no such workout has no row. The caller owns the transaction.
    """
    workouts: dict[date, list[tuple]] = defaultdict(list)
    for local_date, duration_s, rating, estimate in conn.execute(
        "SELECT local_date, json_extract(chosen_json, '$.duration_s.value'),"
        " json_extract(chosen_json, '$.effort_rating.value'),"
        " json_extract(chosen_json, '$.effort_estimate.value')"
        " FROM session_groups"
    ):
        effort = rating if rating is not None else estimate
        workouts[date.fromisoformat(local_date)].append((duration_s, effort))
    loads: dict[date, float] = {}
    rows: dict[date, dict] = {}
    for day, found in workouts.items():
        rated = [(d, e) for d, e in found if d and e]
        if not rated:
            continue
        loads[day] = sum(d / 60 * e for d, e in rated)
        rows[day] = {"workouts": len(rated)}
        if without := len(found) - len(rated):
            rows[day]["note"] = f"{without} workout{'s' * (without > 1)} without effort"
    conn.execute("DELETE FROM scores WHERE score = ?", (EFFORT_SCORE,))
    for day in sorted(loads):
        previous = [
            loads[past]
            for offset in range(1, _EFFORT["baseline_days"] + 1)
            if (past := day - timedelta(days=offset)) in loads
        ]
        rows[day] |= {
            "normal": round(median(previous)) if previous else None,
            "normal_days": len(previous),
        }
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            (
                day.isoformat(),
                EFFORT_SCORE,
                round(loads[day]),
                json.dumps(rows[day]),
                EFFORT_FORMULA_VERSION,
            ),
        )
    return len(loads)
