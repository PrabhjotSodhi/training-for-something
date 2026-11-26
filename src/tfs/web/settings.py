"""Read-only connection details for the Settings page."""

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Literal

from pydantic import BaseModel

from tfs import __version__, providers
from tfs.connectors.core.contract import load_manifests
from tfs.persistence.sqlite import connect


class AppStatus(BaseModel):
    label: str
    connected: bool
    last_received_at: str | None


class ToolStatus(BaseModel):
    label: str
    status: Literal["Connected", "Found, not connected", "Not installed"] | None


class PairedPhone(BaseModel):
    device: str | None
    paired_at: str | None


class Settings(BaseModel):
    apps: list[AppStatus]
    tools: list[ToolStatus]
    phones: list[PairedPhone]
    version: str


def settings_reader(db: Path) -> Callable[[bool], Settings]:
    """Share one minute of tool checks across both screen listeners."""
    lock = Lock()
    expires = 0.0
    cached: list[ToolStatus] = []

    def read(check_tools: bool = True) -> Settings:
        nonlocal cached, expires
        if check_tools:
            with lock:
                if monotonic() >= expires:
                    programs, connected = providers.detect(Path.home())
                    cached = [
                        ToolStatus(
                            label=tool.label,
                            status="Connected"
                            if tool in connected
                            else "Found, not connected"
                            if program
                            else "Not installed",
                        )
                        for tool, program in programs.items()
                    ]
                    expires = monotonic() + 60
                tools = cached
        else:
            tools = [ToolStatus(label=tool.label, status=None) for tool in providers.ALL_TOOLS]
        with closing(connect(db, "read_only")) as conn:
            enabled = dict(
                conn.execute("SELECT connector, last_received_at FROM sources WHERE enabled = 1")
            )
            phones = [
                PairedPhone(device=device, paired_at=paired_at)
                for device, paired_at in conn.execute(
                    "SELECT device, paired_at FROM phone_pairings WHERE code_expires_at IS NULL"
                    " ORDER BY paired_at DESC, rowid"
                )
            ]
        apps = [
            AppStatus(
                label=manifest["label"],
                connected=name in enabled,
                last_received_at=enabled.get(name),
            )
            for name, manifest in load_manifests().items()
        ]
        return Settings(apps=apps, tools=tools, phones=phones, version=__version__)

    return read
