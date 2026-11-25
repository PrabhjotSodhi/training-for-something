"""`tfs update`: install the newest TFS release and bring TFS back."""

import argparse
import contextlib
import ctypes
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from tfs import __version__, config
from tfs.connectors.core.runner import http_get
from tfs.engine import tfs_answers_ping
from tfs.providers import PROVIDERS, mcp_tools

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/PrabhjotSodhi/training-for-something/releases"
RUNNING_FILE = "running"
CHECK_FILE = "update-check.json"
CHECK_EVERY_SECONDS = 24 * 60 * 60
POLL_SECONDS = 1
STOP_TIMEOUT_SECONDS = 30
PROCESS_QUERY_SECONDS = 30
_SYNCHRONIZE = 0x00100000
_RETRY = "TFS could not update. To try again, stop TFS if it runs, then run:"
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
        uv=shutil.which("uv"),
        helpers=_helpers if windows else list,
        stop=_stop,
        install=_hand_off if windows else _install,
    )


class _EarlyExitError(Exception):
    """Ends the update early with an exit code and the message that says why."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def update(
    current: str,
    *,
    fetch: Callable[[str], bytes],
    uv: str | None,
    helpers: Callable[[], list[str]],
    stop: Callable[[], bool],
    install: Callable[[str, Path, bool], int],
) -> int:
    """Install the newest release this install takes, if it is newer, and return the exit code.

    uv is the path of uv, if found. helpers names the AI tool of each running `tfs mcp` that
    the install would break. stop stops a running `tfs start`, says whether one ran and raises
    TimeoutError if it does not end in time. install gets uv, the wheel and whether to start
    TFS again, and returns the exit code.
    """
    try:
        version, name, wheel_bytes = _download(current, fetch, uv)
        _wait_for(helpers)
        restart = _stop_with(stop)
    except _EarlyExitError as ended:
        print(ended, file=sys.stderr if ended.code else sys.stdout)
        return ended.code
    wheel = Path(tempfile.mkdtemp()) / Path(name).name
    wheel.write_bytes(wheel_bytes)
    print(f"Installing TFS {version}", flush=True)
    try:
        return install(uv, wheel, restart)
    except OSError as error:
        print(f"Could not install TFS: {error}", file=sys.stderr)
        return 1


def _download(
    current: str, fetch: Callable[[str], bytes], uv: str | None
) -> tuple[str, str, bytes]:
    """Return the version, file name and verified bytes of the wheel to install."""
    try:
        found = _newest(json.loads(fetch(RELEASES_URL)), current)
        if found is None:
            raise _EarlyExitError(0, f"You have the latest TFS, {current}.")
        if uv is None:
            raise _EarlyExitError(
                1, "TFS needs uv to update. Install uv, then run tfs update again."
            )
        version, asset = found
        print(f"Downloading TFS {version}", flush=True)
        wheel_bytes = fetch(asset["browser_download_url"])
    except (OSError, ValueError, LookupError, TypeError) as error:
        raise _EarlyExitError(1, f"Could not read the TFS releases on GitHub: {error}") from error
    if asset.get("digest") != "sha256:" + hashlib.sha256(wheel_bytes).hexdigest():
        raise _EarlyExitError(
            1, "The download does not match the release, so TFS installed nothing."
        )
    return version, asset["name"], wheel_bytes


def _wait_for(helpers: Callable[[], list[str]]) -> None:
    """Name each AI tool that uses TFS and wait until none does."""
    try:
        if names := helpers():
            for name in sorted(set(names)):
                print(f"{name} is using TFS. Close it, and the update continues.", flush=True)
            while helpers():
                sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        raise _EarlyExitError(1, "Update cancelled. TFS is unchanged.") from None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise _EarlyExitError(1, f"Could not check which programs use TFS: {error}") from error


def _stop_with(stop: Callable[[], bool]) -> bool:
    """Stop TFS and say whether it ran."""
    try:
        return stop()
    except KeyboardInterrupt:
        raise _EarlyExitError(
            1, "Update cancelled after TFS was told to stop. Run tfs start to use TFS."
        ) from None
    except TimeoutError:
        raise _EarlyExitError(
            1,
            f"TFS did not stop within {STOP_TIMEOUT_SECONDS} seconds, so the update installed"
            " nothing. Run tfs update again once TFS has stopped.",
        ) from None


def _newest(releases: list[dict], current: str) -> tuple[str, dict] | None:
    """Return the version and wheel of the highest release this install takes, if newer."""
    takes_prereleases = _key(current)[1] < _STAGES.index("")
    eligible = []
    for release in releases:
        version = release["tag_name"].removeprefix("v")
        wheels = [
            asset
            for asset in release["assets"]
            if asset["name"].startswith(f"training_for_something-{version}-")
            and asset["name"].endswith(".whl")
        ]
        if not release["draft"] and (takes_prereleases or not release["prerelease"]) and wheels:
            eligible.append((version, wheels[0]))
    newest = max(eligible, key=lambda found: _key(found[0]), default=None)
    return newest if newest and _key(newest[0]) > _key(current) else None


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
    unfinished = data_dir / f"{CHECK_FILE}.tmp"
    try:
        unfinished.write_text(json.dumps(cache), encoding="utf-8")
        unfinished.replace(data_dir / CHECK_FILE)
    except OSError as error:
        logger.warning("TFS could not save its update check, and tries again tomorrow: %s", error)
    return f"TFS {newer} is ready. Run tfs update." if newer and newer != told else None


def available(data_dir: Path) -> str | None:
    """Return the newer version that the last check found for this install, or None."""
    cache = _cache(data_dir)
    return cache.get("newer") if cache.get("current") == __version__ else None


def _cache(data_dir: Path) -> dict:
    try:
        cache = json.loads((data_dir / CHECK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _processes() -> dict[int, dict]:
    """List this computer's processes by id."""
    listing = subprocess.run(
        [shutil.which("powershell") or "powershell", "-NoProfile", "-Command", _PROCESSES],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=PROCESS_QUERY_SECONDS,
    ).stdout
    return {process["ProcessId"]: process for process in json.loads(listing)}


def _helpers() -> list[str]:
    """Name the AI tool that runs each `tfs mcp` from this install, from its parent process."""
    processes = _processes()
    names = []
    for process in processes.values():
        line = (process["CommandLine"] or "").lower()
        launcher = processes.get(process["ParentProcessId"], {})
        # tfs.exe starts this install's python, whose command line names it and ends in mcp.
        if (
            sys.executable.lower() in line
            and line.split()[-1].strip('"') == "mcp"
            and launcher.get("Name") == "tfs.exe"
        ):
            tool = processes.get(launcher["ParentProcessId"], {}).get("Name") or ""
            names.append(_AI_TOOLS.get(tool.lower().removesuffix(".exe"), "An AI tool"))
    return names


def _updater_ids(processes: dict[int, dict]) -> list[int]:
    """Return this process and its parents up to the tfs.exe that started it."""
    ids = [os.getpid()]
    parent = processes.get(processes.get(os.getpid(), {}).get("ParentProcessId"))
    while parent and parent["Name"].lower() in ("python.exe", "tfs.exe"):
        ids.append(parent["ProcessId"])
        if parent["Name"].lower() == "tfs.exe":
            break
        parent = processes.get(parent["ParentProcessId"])
    return ids


def _stop() -> bool:
    """Stop a running `tfs start`, wait until it has ended and say whether one ran."""
    if not tfs_answers_ping(config.SCREEN_PORT):
        return False
    running = config.data_dir() / RUNNING_FILE
    try:
        start_id = int(running.read_text())
    except (OSError, ValueError):
        print("Press Ctrl+C where TFS runs, and the update continues.", flush=True)
        while tfs_answers_ping(config.SCREEN_PORT):
            sleep(POLL_SECONDS)
        return True
    running.unlink(missing_ok=True)
    print("Stopping TFS", flush=True)
    if not _ended(start_id, STOP_TIMEOUT_SECONDS):
        raise TimeoutError
    return True


def _ended(process_id: int, timeout: float) -> bool:
    """Wait up to timeout seconds for the process to end and say whether it did."""
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, process_id)
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return True
        sleep(0.1)
    return False


def _install(uv: str, wheel: Path, restart: bool) -> int:
    installed = (
        subprocess.run([uv, "tool", "install", "--force", wheel], check=False).returncode == 0
    )
    if installed:
        shutil.rmtree(wheel.parent)
    else:
        print(f"{_RETRY} uv tool install --force {shlex.quote(str(wheel))}", file=sys.stderr)
    if restart:
        start = subprocess.Popen([shutil.which("tfs") or "tfs", "start", "--open"])
        # Ctrl+C reaches both processes. This one waits while TFS stops cleanly.
        with contextlib.suppress(KeyboardInterrupt):
            start.wait()
        start.wait()
    return 0 if installed else 1


def _hand_off(uv: str, wheel: Path, restart: bool) -> int:
    """Install from a new window once this process ends, because Windows locks a running tfs.exe."""
    try:
        waits = ", ".join(str(process_id) for process_id in _updater_ids(_processes()))
    except (OSError, subprocess.SubprocessError, ValueError):
        # Without the process list, wait for this process and the one that started it.
        waits = f"{os.getpid()}, {os.getppid()}"
    retry = f"uv tool install --force {_quoted(wheel)}"
    start = (
        f"Write-Host 'Keep this window open.'; & {_quoted(shutil.which('tfs') or 'tfs')} start --open"
        if restart
        else ""
    )
    script = (
        f"Wait-Process -Id {waits} -ErrorAction SilentlyContinue\n"
        f"& {_quoted(uv)} tool install --force {_quoted(wheel)}\n"
        "if ($? -and $LASTEXITCODE -eq 0) {"
        f" Remove-Item -LiteralPath {_quoted(wheel.parent)} -Recurse; Write-Host 'TFS is updated.' }}\n"
        f"else {{ Write-Host {_quoted(_RETRY)}; Write-Host {_quoted(retry)} }}\n"
        f"{start}\n"
    )
    subprocess.Popen(
        [shutil.which("powershell") or "powershell", "-NoExit", "-NoProfile", "-Command", script],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    print("TFS finishes updating in a new window.")
    return 0


def _quoted(text: object) -> str:
    """Quote text as a PowerShell string that expands nothing."""
    return "'" + str(text).replace("'", "''") + "'"
