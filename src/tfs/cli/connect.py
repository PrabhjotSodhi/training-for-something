"""`tfs connect NAME`: ask for what the app needs, check it and turn the source on."""

import argparse
import contextlib
import getpass
import sys
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

from tfs import config, password_store
from tfs.connectors.core import runner
from tfs.connectors.core.contract import load_manifest, load_manifests, to_utc_text
from tfs.persistence import sqlite
from tfs.persistence.migrations import BackupFailedError, DatabaseTooNewError, migrate
from tfs.scheduling import jobs

_CONNECT_TIMEOUT_S = 60


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `connect` command."""
    parser = subparsers.add_parser("connect", help="connect an app and turn its source on")
    parser.add_argument("name", help="the connector id, for example hevy")
    parser.add_argument("--module", help=argparse.SUPPRESS)
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Connect the named app and return the exit code."""
    manifest = _manifest(args.name, args.module)
    if manifest is None:
        print(f"No connector named {args.name}.", file=sys.stderr)
        return 2
    answers = {
        ask["key"]: (getpass.getpass if ask["secret"] else input)(f"{ask['label']}: ")
        for ask in manifest["asks"]
    }
    try:
        connect_source(args.name, answers, module=args.module)
    except (
        runner.ConnectorUserError,
        runner.RunnerTimeoutError,
        runner.RunnerFailedError,
        password_store.PasswordStoreError,
        DatabaseTooNewError,
        BackupFailedError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Connected {manifest['label']}. Run tfs sync to fetch your data.")
    return 0


def connect_source(name: str, answers: dict[str, str], *, module: str | None = None) -> None:
    """Check the answers with the connector, store the secrets it returns and enable the source."""
    result = runner.run(
        name, "connect", {"answers": answers}, timeout_s=_CONNECT_TIMEOUT_S, module=module
    )
    secrets = {} if result.get("unsupported") else result.get("secrets", {})
    for key, value in secrets.items():
        password_store.set(f"{name}:{key}", value)
    config.data_dir().mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite.connect(config.db_path())) as conn:
        migrate(conn)
        with sqlite.transaction(conn):
            conn.execute(
                "INSERT INTO sources (connector, enabled) VALUES (?, 1)"
                " ON CONFLICT (connector) DO UPDATE SET enabled = 1",
                (name,),
            )
            jobs.resume_auth(conn, name, to_utc_text(datetime.now(UTC)))


def _manifest(name: str, module: str | None) -> dict | None:
    if module is None:
        return load_manifests().get(name)
    spec = find_spec(module)
    if spec is None or spec.origin is None:
        raise ValueError(f"The connector module {module} cannot be found.")
    return load_manifest(Path(spec.origin).parent / "manifest.json")
