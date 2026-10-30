import json
import time
from pathlib import Path

RECORD = {
    "kind": "measurement",
    "stream": "records",
    "key": "k1",
    "revision_at": None,
    "start_utc": "2026-02-28T18:12:33Z",
    "end_utc": "2026-02-28T18:20:00Z",
    "utc_offset_min": 780,
    "local_date": "2026-03-01",
    "label": "Steps",
    "family": None,
    "metric": "steps",
    "values": {"value": 532.0, "unit": "count"},
    "origin_app": "Fake app",
    "origin_device": None,
    "plan_ref": None,
}


def read(path: str, fmt: str, out_path: str) -> dict:
    Path(out_path).write_text(json.dumps(RECORD) + "\n", encoding="utf-8")
    return {"status": "ok", "count": 1, "message": None}


def send(**_: object) -> dict:
    time.sleep(30)
    return {}


def connect(answers: dict) -> dict:
    raise ValueError(f"The key {answers['api_key']} was refused.")
