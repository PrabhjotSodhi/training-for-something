"""Builds the daily facts: sums that count each moment once, and the latest reading of the day (HLD 7.3, 8)."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, time, timedelta, tzinfo

from tfs.connectors.core.contract import METRICS, parse_iso_time

FORMULA_VERSION = "day-facts-1"

# A source is connector/stream/origin_app/origin_device. Ties on the instant go to the smaller
# source and then to the smaller record id, so the result never depends on insert order.
_LATEST = """
INSERT INTO day_facts (local_date, metric, value, unit, source, coverage, formula_version)
SELECT local_date, :metric, value, :unit, source, 'full', :formula_version FROM (
  SELECT local_date, value, source,
         row_number() OVER (PARTITION BY local_date ORDER BY start_utc DESC, source, id) AS position
  FROM (
    SELECT id, local_date, start_utc,
           json_extract(values_json, '$.value') AS value,
           connector || '/' || stream || '/' || coalesce(origin_app, '-') || '/'
             || coalesce(origin_device, '-') AS source
    FROM source_records
    WHERE kind = 'measurement' AND metric = :metric AND deleted_at IS NULL
      AND local_date IN (SELECT value FROM json_each(:days))
  )
  WHERE NOT (:zero_is_missing AND value = 0)
) WHERE position = 1
"""

_SUMMED = """
SELECT local_date, metric, connector, start_utc, end_utc,
       json_extract(values_json, '$.value'), coalesce(json_extract(values_json, '$.merged'), 0),
       connector || '/' || stream || '/' || coalesce(origin_app, '-') || '/'
         || coalesce(origin_device, '-')
FROM source_records
WHERE kind = 'measurement' AND deleted_at IS NULL
  AND metric IN (SELECT value FROM json_each(:metrics))
  AND local_date IN (SELECT value FROM json_each(:days))
"""


def _uncovered_seconds(start: float, end: float, covered: list[tuple[float, float]]) -> float:
    free, gap = start, 0.0
    for covered_start, covered_end in sorted(covered):
        if covered_end <= free:
            continue
        if covered_start >= end:
            break
        gap += max(0.0, covered_start - free)
        free = covered_end
    return gap + max(0.0, end - free)


def _counted(
    by_source: dict[str, list[tuple]], merge_by_time: bool
) -> tuple[list[tuple[str, float]], bool]:
    """Return the sources one connector counts for a day, with their values, and if the first is marked merged."""
    totals = {source: sum(record[2] for record in records) for source, records in by_source.items()}
    marked = [source for source, records in by_source.items() if any(r[3] for r in records)]
    if marked:
        best = min(marked, key=lambda source: (-totals[source], source))
        return [(best, totals[best])], True
    order = sorted(by_source, key=lambda source: (-totals[source], source))
    spans = {
        source: [
            (parse_iso_time(r[0]).timestamp(), r[1] and parse_iso_time(r[1]).timestamp())
            for r in by_source[source]
        ]
        for source in order
    }
    if not merge_by_time or any(
        end is None or end <= start for source in order for start, end in spans[source]
    ):
        return [(order[0], totals[order[0]])], False
    counted, covered = [], []
    for source in order:
        added = 0.0
        for (start, end), record in zip(spans[source], by_source[source], strict=True):
            added += record[2] * _uncovered_seconds(start, end, covered) / (end - start)
        if added > 0 or not counted:
            counted.append((source, added))
        covered += spans[source]
    return counted, False


def _day_end(date: str, tz: tzinfo | None) -> datetime:
    midnight = datetime.combine(datetime.fromisoformat(date) + timedelta(days=1), time())
    return midnight.replace(tzinfo=tz) if tz else midnight.astimezone()


def rebuild(conn: sqlite3.Connection, dates: set[str], *, tz: tzinfo | None = None) -> int:
    """Rebuild the day facts of dates and return how many rows that wrote.

    Every metric with a daily rule is cleared first, so a day that lost its readings loses its
    row. A summed day is partial until its connector has received data past the end of that date
    in tz, or in the computer's zone when tz is None. The caller owns the transaction.
    """
    days = json.dumps(sorted(dates))
    daily = {name: metric for name, metric in METRICS.items() if metric.daily != "none"}
    conn.execute(
        "DELETE FROM day_facts WHERE local_date IN (SELECT value FROM json_each(?))"
        " AND metric IN (SELECT value FROM json_each(?))",
        (days, json.dumps(list(daily))),
    )
    written = 0
    summed = {name: metric for name, metric in daily.items() if metric.daily == "sum"}
    received = dict(conn.execute("SELECT connector, last_received_at FROM sources"))
    grouped: dict[tuple[str, str], dict[str, dict[str, list[tuple]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for date, name, connector, start, end, value, merged, source in conn.execute(
        _SUMMED, {"metrics": json.dumps(list(summed)), "days": days}
    ):
        grouped[date, name][connector][source].append((start, end, value, merged))
    for (date, name), connectors in grouped.items():
        counted = {
            connector: _counted(by_source, name != "sleep_asleep")
            for connector, by_source in connectors.items()
        }
        connector = min(
            counted,
            key=lambda c: (not counted[c][1], -sum(v for _, v in counted[c][0]), c),
        )
        sources, _ = counted[connector]
        value = sum(added for _, added in sources)
        if summed[name].zero_is_missing and value == 0:
            continue
        arrived = received.get(connector)
        partial = name != "sleep_asleep" and (
            arrived is None or parse_iso_time(arrived) < _day_end(date, tz)
        )
        conn.execute(
            "INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,"
            " formula_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                date,
                name,
                value,
                summed[name].unit,
                " + ".join(source for source, _ in sources),
                "partial" if partial else "full",
                FORMULA_VERSION,
            ),
        )
        written += 1
    for name, metric in daily.items():
        if metric.daily == "latest":
            written += conn.execute(
                _LATEST,
                {
                    "metric": name,
                    "unit": metric.unit,
                    "zero_is_missing": metric.zero_is_missing,
                    "formula_version": FORMULA_VERSION,
                    "days": days,
                },
            ).rowcount
    return written
