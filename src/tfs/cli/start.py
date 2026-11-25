"""`tfs start`: run the engine in the foreground until Ctrl+C."""

import argparse
import socket
import sqlite3
import sys
import threading
import webbrowser
from contextlib import closing
from time import sleep

from tfs import config, password_store
from tfs.cli import update
from tfs.connectors.core.contract import load_manifests
from tfs.engine import Engine, EngineAlreadyRunningError, PortInUseError
from tfs.persistence import sqlite
from tfs.persistence.migrations import BackupFailedError, DatabaseTooNewError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `start` command."""
    parser = subparsers.add_parser("start", help="run TFS until you press Ctrl+C")
    parser.add_argument(
        "--home-wifi",
        action="store_true",
        help="also take pushes from phones on your home Wi-Fi",
    )
    parser.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(run=run)


def run(args: argparse.Namespace, *, footer: str = "") -> int:
    """Run the engine until Ctrl+C and return the exit code. Print the footer under the banner."""
    home_wifi = args.home_wifi or _push_source_enabled()
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
        running.touch()
        manifests = load_manifests()
        push_connectors = [
            (name, manifest["label"])
            for name, manifest in manifests.items()
            if manifest["routes"]["push"]
        ]
        home_wifi_host = f"{socket.gethostname()}.local" if home_wifi else None
        home_wifi_port = config.HOME_WIFI_PORT if home_wifi else None
        print(
            render_banner(
                config.SCREEN_HOST,
                config.SCREEN_PORT,
                home_wifi_host,
                home_wifi_port,
                password_store.get("push_token") or "",
                push_connectors,
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


def _push_source_enabled() -> bool:
    """Say whether any turned-on source takes pushes from a phone."""
    if not config.db_path().exists():
        return False
    push_connectors = {
        name for name, manifest in load_manifests().items() if manifest["routes"]["push"]
    }
    with closing(sqlite.connect(config.db_path(), "read_only")) as conn:
        try:
            rows = conn.execute("SELECT connector FROM sources WHERE enabled = 1").fetchall()
        except sqlite3.OperationalError:
            return False
    return any(connector in push_connectors for (connector,) in rows)


def render_banner(
    host: str,
    port: int,
    home_wifi_host: str | None,
    home_wifi_port: int | None,
    token: str,
    push_connectors: list[tuple[str, str]],
) -> str:
    """Build the text that says TFS is running, with a push URL for each app that takes pushes."""
    lines = ["TFS is running. Press Ctrl+C to stop."]
    for name, label in push_connectors:
        lines.append(label)
        lines.append(f"  Push URL (this computer): http://{host}:{port}/push/{name}")
        if home_wifi_host is not None:
            lines.append(
                f"  Push URL (home Wi-Fi):    http://{home_wifi_host}:{home_wifi_port}/push/{name}"
            )
    lines.append(f"Push token: {token}   Keep this secret.")
    return "\n".join(lines)
