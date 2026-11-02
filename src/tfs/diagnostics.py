"""One status report for the screens and `tfs status`: what came in, what is stale, what broke."""

import sqlite3
from datetime import datetime, timedelta

from tfs.connectors.core.contract import load_manifests

_FILE_STATUSES = ("new", "reading", "ok", "partial", "unreadable", "needs_ai", "failed", "missing")
_PROBLEM_LIMIT = 20


def report(conn: sqlite3.Connection, *, now: str, manifests: dict | None = None) -> dict:
    """Read sources and files into a report. A source is stale past its stale_after_hours."""
    manifests = load_manifests() if manifests is None else manifests
    sources = []
    for row in conn.execute(
        "SELECT connector, enabled, last_attempt_at, last_contact_at, last_received_at,"
        " last_error, (SELECT MAX(COALESCE(end_utc, start_utc)) FROM source_records"
        "  WHERE connector = sources.connector AND deleted_at IS NULL) AS covered_until"
        " FROM sources ORDER BY connector"
    ):
        manifest = manifests.get(row["connector"], {})
        sources.append(
            {
                "connector": row["connector"],
                "label": manifest.get("label", row["connector"]),
                "enabled": bool(row["enabled"]),
                "last_attempt_at": row["last_attempt_at"],
                "last_contact_at": row["last_contact_at"],
                "last_received_at": row["last_received_at"],
                "covered_until": row["covered_until"],
                "stale": _is_stale(row["last_contact_at"], manifest.get("stale_after_hours"), now),
                "last_error": row["last_error"],
            }
        )
    files = dict.fromkeys(_FILE_STATUSES, 0)
    for row in conn.execute("SELECT status, COUNT(*) AS total FROM files GROUP BY status"):
        files[row["status"]] = row["total"]
    problems = [
        dict(row)
        for row in conn.execute(
            "SELECT sha256, connector, status, message FROM files"
            " WHERE status IN ('partial', 'unreadable', 'failed', 'missing')"
            " ORDER BY arrival_seq DESC LIMIT ?",
            (_PROBLEM_LIMIT,),
        )
    ]
    return {"sources": sources, "files": files, "problems": problems}


def render_text(report: dict) -> str:
    """Build the plain text of a report: one line per source, then the file problems."""
    width = max((len(source["label"]) for source in report["sources"]), default=0)
    lines = []
    for source in report["sources"]:
        if source["stale"]:
            since = source["last_contact_at"]
            state = "Stale: no contact " + (f"since {_short(since)}" if since else "yet")
        else:
            state = f"OK: last contact {_short(source['last_contact_at'])}"
        if source["covered_until"]:
            state += f", data until {_short(source['covered_until'])}"
        if source["last_error"]:
            state += f". Last error: {source['last_error']}"
        lines.append(f"{source['label'].ljust(width)}   {state}")
    counts = [f"{count} {status}" for status, count in report["files"].items() if count]
    lines.append("Files: " + (", ".join(counts) if counts else "none"))
    for problem in report["problems"]:
        lines.append(
            f"  {problem['status']} {problem['connector']} {problem['sha256'][:12]}:"
            f" {problem['message'] or 'no message'}"
        )
    return "\n".join(lines)


def _is_stale(last_contact_at: str | None, stale_after_hours: int | None, now: str) -> bool:
    if last_contact_at is None:
        return True
    if stale_after_hours is None:
        return False
    limit = timedelta(hours=stale_after_hours)
    return datetime.fromisoformat(now) - datetime.fromisoformat(last_contact_at) > limit


def _short(utc: str) -> str:
    return datetime.fromisoformat(utc).strftime("%Y-%m-%d %H:%M UTC")
