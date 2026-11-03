"""The push endpoint: phone apps post a file with a bearer token and TFS saves it."""

import contextlib
import hmac
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from tfs import password_store
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.importer.raw_store import save_raw
from tfs.web.server import PushHandler


def make_push_handler(
    *,
    data_dir: Path,
    conn_factory: Callable[[], sqlite3.Connection],
    now_fn: Callable[[], datetime],
    on_saved: Callable[[str], None],
) -> PushHandler:
    """Build the handler for make_server. It answers 200 only after the raw file is registered."""
    manifests = load_manifests()

    def handle(name: str, body: BinaryIO, headers) -> tuple[int, dict]:
        token = password_store.get("push_token")
        authorization = headers.get("Authorization", "").encode()
        # Bytes, because compare_digest rejects text that is not ASCII.
        if not token or not hmac.compare_digest(authorization, f"Bearer {token}".encode()):
            return 401, {"ok": False, "error": "The push token is missing or wrong."}
        manifest = manifests.get(name)
        if manifest is None or not manifest["routes"]["push"]:
            return 404, {"ok": False, "error": "This app does not take pushes."}
        with contextlib.closing(conn_factory()) as conn:
            saved = save_raw(
                conn,
                data_dir,
                name,
                "rest_json",
                "json",
                body,
                now=to_utc_text(now_fn()),
            )
        on_saved(saved.sha256)
        return 200, {"ok": True, "sha256": saved.sha256}

    return handle
