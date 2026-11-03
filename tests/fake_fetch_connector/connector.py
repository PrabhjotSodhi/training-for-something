import json
import os
from pathlib import Path


def fetch(cursor: dict | None, secrets: dict, out_dir: str, http, now: str) -> dict:
    """Write the pages named by the FAKE_FETCH plan, or fail the way the plan says."""
    plan = json.loads(os.environ["FAKE_FETCH"])
    if plan.get("raise"):
        raise RuntimeError(f"request rejected for key {secrets['api_key']}")
    files = []
    for number in range(1, plan["files"] + 1):
        path = Path(out_dir) / f"page-{number}.json"
        path.write_text(f"page {number}", encoding="utf-8")
        files.append({"path": str(path), "fmt": "page", "ext": "json"})
    return {
        "files": files,
        "cursor": plan["cursor"],
        "auth_failed": plan.get("auth_failed", False),
        "wait_until": None,
    }


def read(path: str, fmt: str, out_path: str) -> dict:
    """Write one event record whose key is the page text."""
    record = {
        "kind": "event",
        "stream": "events",
        "key": Path(path).read_text(encoding="utf-8"),
        "revision_at": None,
        "start_utc": "2026-10-26T18:00:00Z",
        "end_utc": None,
        "utc_offset_min": 0,
        "local_date": "2026-10-26",
        "label": "Invented event",
        "family": None,
        "metric": None,
        "values": {},
        "origin_app": None,
        "origin_device": None,
        "plan_ref": None,
    }
    Path(out_path).write_text(json.dumps(record) + "\n", encoding="utf-8")
    return {"status": "ok", "count": 1, "message": None}
