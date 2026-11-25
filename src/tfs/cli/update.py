"""`tfs update`: install the newest TFS release and bring TFS back."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import sleep

from tfs import __version__, config
from tfs.connectors.core.runner import http_get
from tfs.engine import tfs_answers_ping
from tfs.providers import PROVIDERS, mcp_tools

RELEASES_URL = "https://api.github.com/repos/PrabhjotSodhi/training-for-something/releases"
RUNNING_FILE = "running"
CHECK_FILE = "update-check.json"
CHECK_EVERY_SECONDS = 24 * 60 * 60
POLL_SECONDS = 1
_VERSION = re.compile(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?")
_STAGES = ("a", "b", "rc", "")
_AI_TOOLS = {tool.command: tool.label for tool in (*PROVIDERS, *mcp_tools.TOOLS) if tool.command}
_PROCESSES = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process"
    " | Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress"
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `update` command."""
    parser = subparsers.add_parser("update", help="install the newest TFS release")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Update this install and return the exit code."""
    windows = sys.platform == "win32"
    return update(
        __version__,
        fetch=_fetch,
        helpers=_helpers if windows else list,
        stop=_stop,
        install=_hand_off if windows else _install,
    )


def update(
    current: str,
    *,
    fetch: Callable[[str], bytes],
    helpers: Callable[[], list[str]],
    stop: Callable[[], bool],
    install: Callable[[Path, bool], None],
) -> int:
    """Install the newest release this install takes, if it is newer, and return the exit code.

    helpers names the AI tool of each running `tfs mcp` that the install would break.
    stop stops a running `tfs start` and says whether one ran. install gets the wheel and
    whether to start TFS again.
    """
    try:
        found = _newest(json.loads(fetch(RELEASES_URL)), current)
        if found is None:
            print(f"You have the latest TFS, {current}.")
            return 0
        version, asset = found
        print(f"Downloading TFS {version}", flush=True)
        wheel_bytes = fetch(asset["browser_download_url"])
    except (OSError, ValueError, LookupError, TypeError) as error:
        print(f"Could not read the TFS releases on GitHub: {error}", file=sys.stderr)
        return 1
    if asset.get("digest") != "sha256:" + hashlib.sha256(wheel_bytes).hexdigest():
        print("The download does not match the release, so TFS installed nothing.", file=sys.stderr)
        return 1
    try:
        told: set[str] = set()
        while names := set(helpers()):
            for name in sorted(names - told):
                print(f"{name} is using TFS. Close it, and the update continues.", flush=True)
            told |= names
            sleep(POLL_SECONDS)
        restart = stop()
    except KeyboardInterrupt:
        print("Update cancelled. TFS is unchanged.", file=sys.stderr)
        return 1
    wheel = Path(tempfile.mkdtemp()) / Path(asset["name"]).name
    wheel.write_bytes(wheel_bytes)
    print(f"Installing TFS {version}", flush=True)
    try:
        install(wheel, restart)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Could not install TFS: {error}", file=sys.stderr)
        return 1
    return 0


def _newest(releases: list[dict], current: str) -> tuple[str, dict] | None:
    """Return the version and wheel of the newest release this install takes, if newer."""
    takes_prereleases = _key(current)[1] < _STAGES.index("")
    for release in releases:
        wheels = [asset for asset in release["assets"] if asset["name"].endswith(".whl")]
        if release["draft"] or (release["prerelease"] and not takes_prereleases) or not wheels:
            continue
        version = release["tag_name"].removeprefix("v")
        return (version, wheels[0]) if _key(version) > _key(current) else None
    return None


def _key(version: str) -> tuple[list[int], int, int]:
    """Sort key for versions like 1.2.0, 1.2.0a3, 1.2.0b1 and 1.2.0rc2."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"TFS cannot compare the version {version!r}.")
    release, stage, number = match.groups()
    return [int(part) for part in release.split(".")], _STAGES.index(stage or ""), int(number or 0)


def _fetch(url: str) -> bytes:
    status, _, body = http_get(url, {"User-Agent": "tfs"})
    if status != 200:
        raise OSError(f"GitHub answered {status} for {url}")
    return body


def watch(data_dir: Path) -> None:
    """Check for a newer release now and every 24 hours, and print each new notice."""
    while True:
        if notice := check(data_dir):
            print(notice, flush=True)
        sleep(CHECK_EVERY_SECONDS)


def check(data_dir: Path, *, fetch: Callable[[str], bytes] = _fetch) -> str | None:
    """Look for a newer release and keep the answer in the data folder.

    Return the notice the first time a version is found. Offline or with
    TFS_NO_UPDATE_CHECK=1, return None.
    """
    if os.environ.get("TFS_NO_UPDATE_CHECK") == "1":
        return None
    try:
        found = _newest(json.loads(fetch(RELEASES_URL)), __version__)
    except (OSError, ValueError, LookupError, TypeError):
        return None
    newer = found[0] if found else None
    told = _cache(data_dir).get("told")
    cache = {"current": __version__, "newer": newer, "told": newer or told}
    (data_dir / CHECK_FILE).write_text(json.dumps(cache), encoding="utf-8")
    return f"TFS {newer} is ready. Run tfs update." if newer and newer != told else None


def available(data_dir: Path) -> str | None:
    """Return the newer version that the last check found for this install, or None."""
    cache = _cache(data_dir)
    return cache.get("newer") if cache.get("current") == __version__ else None


def _cache(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / CHECK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _helpers() -> list[str]:
    """Name the AI tool that runs each `tfs mcp` from this install, from its parent process."""
    listing = subprocess.run(
        [shutil.which("powershell") or "powershell", "-NoProfile", "-Command", _PROCESSES],
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout
    processes = {process["ProcessId"]: process for process in json.loads(listing)}
    names = []
    for process in processes.values():
        line = (process["CommandLine"] or "").lower().rstrip()
        launcher = processes.get(process["ParentProcessId"], {})
        # tfs.exe starts this install's python, whose command line names it and ends in mcp.
        if (
            sys.executable.lower() in line
            and line.endswith(" mcp")
            and launcher.get("Name") == "tfs.exe"
        ):
            tool = processes.get(launcher["ParentProcessId"], {}).get("Name") or ""
            names.append(_AI_TOOLS.get(tool.lower().removesuffix(".exe"), "An AI tool"))
    return names


def _stop() -> bool:
    """Stop a running `tfs start`, wait until it has stopped and say whether one ran."""
    if not tfs_answers_ping(config.SCREEN_PORT):
        return False
    running = config.data_dir() / RUNNING_FILE
    if running.exists():
        running.unlink(missing_ok=True)
        print("Stopping TFS", flush=True)
    else:
        print("Press Ctrl+C where TFS runs, and the update continues.", flush=True)
    while tfs_answers_ping(config.SCREEN_PORT):
        sleep(POLL_SECONDS)
    return True


def _install(wheel: Path, restart: bool) -> None:
    subprocess.run([shutil.which("uv") or "uv", "tool", "install", "--force", wheel], check=True)
    shutil.rmtree(wheel.parent)
    if restart:
        start = subprocess.Popen([shutil.which("tfs") or "tfs", "start", "--open"])
        # Ctrl+C reaches both processes. This one waits while TFS stops cleanly.
        with contextlib.suppress(KeyboardInterrupt):
            start.wait()
        start.wait()


def _hand_off(wheel: Path, restart: bool) -> None:
    """Install from a new window once this process ends, because Windows locks a running tfs.exe."""
    wheel_path = str(wheel).replace("'", "''")
    then = (
        "Write-Host 'Keep this window open.'; & (Join-Path (uv tool dir --bin) 'tfs.exe') start --open"
        if restart
        else "Write-Host 'TFS is updated. You can close this window.'"
    )
    script = (
        f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue\n"
        f"uv tool install --force '{wheel_path}'\n"
        "if ($LASTEXITCODE) { Write-Host 'TFS could not update. Run the install line again.' }\n"
        f"else {{ Remove-Item -Recurse (Split-Path '{wheel_path}'); {then} }}\n"
    )
    subprocess.Popen(
        [shutil.which("powershell") or "powershell", "-NoExit", "-NoProfile", "-Command", script],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    print("TFS finishes updating in a new window.")
