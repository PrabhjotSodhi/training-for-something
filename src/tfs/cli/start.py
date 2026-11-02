"""`tfs start`: run the engine in the foreground until Ctrl+C."""

import argparse
import socket
import sys
from time import sleep

from tfs import config, password_store
from tfs.connectors.core.contract import load_manifests
from tfs.engine import Engine, EngineAlreadyRunningError, PortInUseError
from tfs.persistence.migrations import DatabaseTooNewError


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `start` command."""
    parser = subparsers.add_parser("start", help="run TFS until you press Ctrl+C")
    parser.add_argument(
        "--home-wifi",
        action="store_true",
        help="also take pushes from phones on your home Wi-Fi",
    )
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Run the engine until Ctrl+C and return the exit code."""
    engine = Engine(config.data_dir(), home_wifi=args.home_wifi)
    try:
        engine.start()
    except (
        EngineAlreadyRunningError,
        PortInUseError,
        DatabaseTooNewError,
        password_store.PasswordStoreError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    try:
        manifests = load_manifests()
        push_connectors = [
            (name, manifest["label"])
            for name, manifest in manifests.items()
            if manifest["routes"]["push"]
        ]
        home_wifi_host = f"{socket.gethostname()}.local" if args.home_wifi else None
        home_wifi_port = config.HOME_WIFI_PORT if args.home_wifi else None
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
        # Windows cannot interrupt a wait with no timeout, so wake up now and then.
        while True:
            sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.stop()


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
