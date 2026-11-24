"""The AI tools TFS connects to. Each has a file here that holds what is specific to it."""

import shutil
import subprocess
from typing import NamedTuple

from tfs.providers import claude_code, codex

REPO = "PrabhjotSodhi/training-for-something"
PLUGIN = "tfs@training-for-something"
_TIMEOUT_S = 120


class Provider(NamedTuple):
    """One AI tool: its name, its program and the plugin install verb it uses."""

    label: str
    command: str
    install: str


PROVIDERS = tuple(Provider(m.LABEL, m.COMMAND, m.INSTALL) for m in (claude_code, codex))


def run_command(argv: list[str]) -> tuple[bool, str]:
    """Run a command and return whether it succeeded and what it printed."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return done.returncode == 0, done.stdout + done.stderr


def find(provider: Provider) -> str | None:
    """Return the full path of the tool's program, or None when it is not installed."""
    return shutil.which(provider.command)


def is_connected(program: str) -> bool:
    """Say whether the tool's plugin list names the TFS plugin."""
    ok, output = run_command([program, "plugin", "list"])
    return ok and PLUGIN in output


def connect(program: str, provider: Provider) -> str | None:
    """Add the TFS marketplace and install the plugin. Return the problem, or None on success."""
    for argv in (
        [program, "plugin", "marketplace", "add", REPO],
        [program, "plugin", provider.install, PLUGIN],
    ):
        ok, output = run_command(argv)
        if not ok:
            lines = output.strip().splitlines()
            return lines[-1] if lines else "the command failed"
    return None
