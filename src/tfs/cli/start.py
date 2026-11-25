"""`tfs start`: run the engine in the foreground until Ctrl+C."""

import argparse
import io
import os
import sqlite3
import sys
import threading
import webbrowser
from contextlib import closing
from datetime import UTC, datetime
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


def run(args: argparse.Namespace, *, footer: str = "", check_phone: bool = True) -> int:
    """Run the engine until Ctrl+C and return the exit code. Print the footer under the banner."""
    home_wifi = args.home_wifi or _home_wifi_enabled()
    data_dir = config.data_dir()
    engine = Engine(data_dir, home_wifi=home_wifi, new_version=lambda: update.available(data_dir))
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
    try:
        running.write_text(str(os.getpid()))
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
                _tailscale.phone_link() if check_phone else None,
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
) -> str:
    """Build the text that says TFS is running, with a push URL for each app that takes pushes."""
    lines = ["TFS is running. Press Ctrl+C to stop."]
    if phone_url:
        lines.append(f"Dashboard (phone): {phone_url}")
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
