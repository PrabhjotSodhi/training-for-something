"""The AI tools TFS connects to. Each has a file here that holds what is specific to it."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from tfs.providers import claude_code, codex, mcp_tools

REPO = "PrabhjotSodhi/training-for-something"
PLUGIN = "tfs@training-for-something"
_TIMEOUT_S = 120
FIRST_MESSAGE = "Use TFS to help me set my first training goal. Ask me one question at a time."
PLAN_MESSAGE = "Use TFS to build my first training plan for my goal. Ask me one question at a time."


class Provider(NamedTuple):
    """One AI tool: its name, its program and the plugin install verb it uses."""

    label: str
    command: str
    install: str


PROVIDERS = tuple(Provider(m.LABEL, m.COMMAND, m.INSTALL) for m in (claude_code, codex))
ALL_TOOLS = (*PROVIDERS, *mcp_tools.TOOLS)


def detect(
    home: Path,
) -> tuple[dict[Provider | mcp_tools.Tool, str | None], set[Provider | mcp_tools.Tool]]:
    """Find each supported tool and check which ones already reach TFS."""
    programs = {tool: find(tool) for tool in PROVIDERS}
    programs.update({tool: mcp_tools.find(tool, home) for tool in mcp_tools.TOOLS})
    connected = {tool for tool in PROVIDERS if programs[tool] and is_connected(programs[tool])} | {
        tool
        for tool in mcp_tools.TOOLS
        if programs[tool] and mcp_tools.is_connected(tool, programs[tool], home, run_command)
    }
    return programs, connected


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


def connect(program: str, provider: Provider, say: Callable[[str], None]) -> str | None:
    """Add the TFS marketplace and install the plugin. Return the problem, or None on success."""
    for step, argv in (
        (
            f"Adding the TFS marketplace to {provider.label}",
            [program, "plugin", "marketplace", "add", REPO],
        ),
        (
            f"Installing the TFS plugin in {provider.label}",
            [program, "plugin", provider.install, PLUGIN],
        ),
    ):
        say(step)
        ok, output = run_command(argv)
        if not ok:
            lines = output.strip().splitlines()
            return lines[-1] if lines else "the command failed"
    return None
