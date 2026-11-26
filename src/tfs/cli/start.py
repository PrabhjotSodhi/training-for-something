"""`tfs start`: run the engine in the foreground until Ctrl+C."""

import argparse
import base64
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import TextIO

import qrcode

from tfs import config, password_store
from tfs.cli import _tailscale, update
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.engine import Engine, EngineAlreadyRunningError, PortInUseError
from tfs.persistence import sqlite
from tfs.persistence.migrations import BackupFailedError, DatabaseTooNewError
from tfs.web.pairing import wifi_host
from tfs.web.server import HomeWifi


def _firewall_script(*, repair: bool) -> str:
    # PowerShell also treats curly quotes in a file name as string delimiters.
    python = sys._base_executable.translate(
        {ord(ch): "'" + ch for ch in "'\u2018\u2019\u201a\u201b"}
    )
    script = f"""$ErrorActionPreference = 'Stop'
$python = '{python}'
$name = 'TFS home Wi-Fi'
"""
    if repair:
        return (
            script
            + f"""$settings = @{{
    Direction = 'Inbound'
    Action = 'Allow'
    Protocol = 'TCP'
    LocalPort = {config.HOME_WIFI_PORT}
    Profile = 'Private'
    RemoteAddress = 'LocalSubnet'
    Program = $python
    Enabled = 'True'
}}
if (Get-NetFirewallRule -Name $name -PolicyStore PersistentStore -ErrorAction SilentlyContinue) {{
    Set-NetFirewallRule -Name $name @settings
}} else {{
    New-NetFirewallRule -Name $name -DisplayName $name @settings
}}
"""
        )
    return (
        script
        + """$rules = Get-NetFirewallRule -PolicyStore ActiveStore
$allow = $rules | Where-Object Name -eq $name
$blocks = @($rules | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' } |
    Where-Object { $_.Profile -eq 'Any' -or ([int]$_.Profile -band 2) } |
    Where-Object {
        $app = ($_ | Get-NetFirewallApplicationFilter).Program
        $app -and [Environment]::ExpandEnvironmentVariables($app) -eq $python
    })
[Console]::OutputEncoding = [Text.Encoding]::UTF8
@{allowed = ($allow.Enabled -eq 'True'); blocks = @($blocks | ForEach-Object { $_.DisplayName })} | ConvertTo-Json -Compress
"""
    )


def _powershell(script: str) -> str:
    program = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _firewall_state() -> tuple[bool, list[str]] | None:
    try:
        state = json.loads(_powershell(_firewall_script(repair=False)))
        return state["allowed"], state["blocks"]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _elevate_firewall() -> None:
    # EncodedCommand passes one Unicode script through both PowerShell command lines.
    script = base64.b64encode(_firewall_script(repair=True).encode("utf-16-le")).decode("ascii")
    _powershell(
        "$ErrorActionPreference = 'Stop'; "
        "$p = Start-Process (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -Wait -PassThru "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {script}'; exit $p.ExitCode"
    )


def _check_firewall(*, setup: bool) -> bool:
    """Report Windows firewall problems and say whether to offer Wi-Fi pairing."""
    if sys.platform != "win32":
        return True
    allowed, blocks = _firewall_state() or (False, [])
    if setup and not allowed and not blocks:
        print(
            "Windows needs your OK once so your phone can reach TFS on your home Wi-Fi.", flush=True
        )
        with suppress(OSError, subprocess.SubprocessError):
            _elevate_firewall()
        allowed, blocks = _firewall_state() or (False, [])
    if blocks:
        if setup:
            print(
                "Windows is blocking Python on home networks, so your phone cannot reach TFS over Wi-Fi."
            )
        else:
            print(
                "Windows is blocking Python on home networks, so phones cannot reach TFS over Wi-Fi. Use Tailscale to view TFS on your phone."
            )
        return False
    if allowed:
        return True
    if setup:
        print("Phone viewing over Wi-Fi will not work until you run tfs setup again.")
    else:
        print("Your phone cannot reach TFS over Wi-Fi. Run tfs setup to fix it.")
    return False


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `start` command."""
    parser = subparsers.add_parser("start", help="run TFS until you press Ctrl+C")
    parser.add_argument(
        "--home-wifi",
        action="store_true",
        help="serve paired phones and take pushes on your home Wi-Fi",
    )
    parser.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(run=run)


def run(
    args: argparse.Namespace,
    *,
    finish_setup: Callable[[HomeWifi], tuple[str, str | None]] | None = None,
) -> int:
    """Start the engine, finish any setup, then show the banner and wait for Ctrl+C."""
    home_wifi = args.home_wifi or _home_wifi_enabled()
    data_dir = config.data_dir()
    engine = Engine(data_dir, home_wifi=home_wifi, new_version=lambda: update.available(data_dir))
    if home_wifi and finish_setup is None:
        _check_firewall(setup=False)
    try:
        engine.start()
    except (
        EngineAlreadyRunningError,
        PortInUseError,
        DatabaseTooNewError,
        BackupFailedError,
        password_store.PasswordStoreError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    if engine.backup_path is not None:
        print(f"Saved a backup of your database before updating it: {engine.backup_path}")
    running = data_dir / update.RUNNING_FILE
    footer = None
    try:
        running.write_text(str(os.getpid()))
        footer, phone_url = (
            finish_setup(engine.home_wifi) if finish_setup else ("", _tailscale.phone_link())
        )
        manifests = load_manifests()
        push_connectors = [
            (name, manifest["label"])
            for name, manifest in manifests.items()
            if manifest["routes"]["push"]
        ]
        home_wifi_host = wifi_host() if home_wifi else None
        home_wifi_port = config.HOME_WIFI_PORT if home_wifi else None
        print(
            render_banner(
                config.SCREEN_HOST,
                config.SCREEN_PORT,
                home_wifi_host,
                home_wifi_port,
                password_store.get("push_token") or "",
                push_connectors,
                phone_url,
                show_phone_qr=finish_setup is None,
            ),
            flush=True,
        )
        if footer:
            print(footer, flush=True)
        threading.Thread(target=update.watch, args=(data_dir,), daemon=True).start()
        if args.open:
            webbrowser.open(f"http://{config.SCREEN_HOST}:{config.SCREEN_PORT}/")
        # Ctrl+C interrupts sleep at once. `tfs update` stops TFS by deleting the running file.
        while running.exists():
            sleep(1)
        print("TFS stopped for an update.", flush=True)
        return 0
    except KeyboardInterrupt:
        if finish_setup and footer is None:
            raise
        return 0
    finally:
        engine.stop()
        running.unlink(missing_ok=True)


def _home_wifi_enabled() -> bool:
    """Enable Wi-Fi for paired phones or sources that take pushes."""
    if not config.db_path().exists():
        return False
    push_connectors = {
        name for name, manifest in load_manifests().items() if manifest["routes"]["push"]
    }
    with closing(sqlite.connect(config.db_path(), "read_only")) as conn:
        try:
            if any(
                row[0] in push_connectors
                for row in conn.execute("SELECT connector FROM sources WHERE enabled = 1")
            ):
                return True
            return (
                conn.execute(
                    "SELECT 1 FROM phone_pairings WHERE code_expires_at IS NULL OR code_expires_at > ? LIMIT 1",
                    (to_utc_text(datetime.now(UTC)),),
                ).fetchone()
                is not None
            )
        except sqlite3.OperationalError:
            return False


def print_qr(url: str, out: TextIO | None = None) -> None:
    """Print a URL as a QR code to the given stream or standard output."""
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    code.add_data(url)
    code.print_ascii(out=out, invert=True)


def _stdout_can_write(text: str) -> bool:
    """Say whether standard output can encode text. Windows output sent to a file may not."""
    try:
        text.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        return False
    return True


def render_banner(
    host: str,
    port: int,
    home_wifi_host: str | None,
    home_wifi_port: int | None,
    token: str,
    push_connectors: list[tuple[str, str]],
    phone_url: str | None = None,
    *,
    show_phone_qr: bool = True,
) -> str:
    """Build the text that says TFS is running, with a push URL for each app that takes pushes."""
    lines = ["TFS is running. Press Ctrl+C to stop."]
    if phone_url:
        lines.append(f"Dashboard (phone): {phone_url}")
    if phone_url and show_phone_qr:
        qr = io.StringIO()
        print_qr(phone_url, qr)
        qr_text = qr.getvalue().rstrip("\n")
        if _stdout_can_write(qr_text):
            lines.append(qr_text)
    for name, label in push_connectors:
        lines.append(label)
        lines.append(f"  Push URL (this computer): http://{host}:{port}/push/{name}")
        if phone_url:
            lines.append(f"  Push URL (Tailscale):     {phone_url}push/{name}")
        if home_wifi_host is not None:
            lines.append(
                f"  Push URL (home Wi-Fi):    http://{home_wifi_host}:{home_wifi_port}/push/{name}"
            )
    lines.append(f"Push token: {token}   Keep this secret.")
    return "\n".join(lines)
