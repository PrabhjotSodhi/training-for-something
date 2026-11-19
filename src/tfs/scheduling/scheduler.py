"""Run every rule that is due, once per occurrence, even after a long sleep."""

import sqlite3
from datetime import timedelta

from tfs.connectors.core.contract import parse_iso_time, to_utc_text
from tfs.persistence.sqlite import transaction


def run_due(conn: sqlite3.Connection, now: str) -> int:
    """Create one run per due rule and return how many were created.

    A rule that slept through several periods runs once, then waits for the next period after now.
    A rule over its hourly cap stays due and runs in the next hour.
    """
    hour = now[:13]
    created = 0
    for rule in conn.execute(
        "SELECT * FROM rules WHERE next_due <= ? ORDER BY next_due, id", (now,)
    ).fetchall():
        runs_this_hour = rule["cap_runs"] if rule["cap_hour"] == hour else 0
        if runs_this_hour >= rule["hourly_cap"]:
            continue
        due = parse_iso_time(rule["next_due"])
        period = timedelta(minutes=rule["period_minutes"])
        missed_periods = (parse_iso_time(now) - due) // period + 1
        with transaction(conn):
            created += conn.execute(
                "INSERT OR IGNORE INTO rule_runs(id, rule_id, created_at) VALUES (?, ?, ?)",
                (f"{rule['id']}:{rule['revision']}:{rule['next_due']}", rule["id"], now),
            ).rowcount
            conn.execute(
                "UPDATE rules SET next_due = ?, cap_hour = ?, cap_runs = ? WHERE id = ?",
                (to_utc_text(due + missed_periods * period), hour, runs_this_hour + 1, rule["id"]),
            )
    return created
