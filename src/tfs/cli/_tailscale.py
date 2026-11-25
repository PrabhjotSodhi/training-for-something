"""Find private phone links and set up Tailscale Serve."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from tfs import config

_TARGET = f"http://{config.SCREEN_HOST}:{config.SCREEN_PORT}"
_APPROVAL_PREFIX = "https://login.tailscale.com/"
_INSTALL_PATHS = {
    "win32": Path("C:/Program Files/Tailscale/tailscale.exe"),
    "darwin": Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
}


def _program() -> str | None:
    if program := shutil.which("tailscale"):
        return program
    path = _INSTALL_PATHS.get(sys.platform)
    return str(path) if path and path.is_file() else None


def _run(program: str, *args: str) -> str | None:
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"} if sys.platform == "darwin" else None
    try:
        result = subprocess.run(
            [program, *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            env=env,
            check=False,
        )
        stdout, stderr = result.stdout, result.stderr
        succeeded = result.returncode == 0
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout or b"", error.stderr or b""
        succeeded = False
    except OSError:
        return None
    output = stdout.decode("utf-8", errors="replace")
    for line in (output + "\n" + stderr.decode("utf-8", errors="replace")).splitlines():
        if line.strip().startswith(_APPROVAL_PREFIX):
            return line.strip()
    return output if succeeded else None


def _read(program: str, *args: str) -> dict | None:
    try:
        data = json.loads(_run(program, *args, "--json") or "")
    except ValueError:
        return None
    # Tailscale can return JSON null when no Serve config exists.
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def _hostname(program: str) -> str:
    status = _read(program, "status") or {}
    if status.get("BackendState") == "Running":
        name = (status.get("Self") or {}).get("DNSName")
        if isinstance(name, str):
            return name.rstrip(".")
    return ""


def _ports(program: str, host: str) -> dict[int, bool] | None:
    """Map occupied ports to whether they privately serve the dashboard."""
    status = _read(program, "serve", "status")
    if status is None:
        return None
    ports = {}
    try:
        for layer in (status, *status.get("Foreground", {}).values()):
            web = layer.get("Web", {})
            tcp = layer.get("TCP", {})
            public = {key for key, enabled in layer.get("AllowFunnel", {}).items() if enabled}
            used = set(tcp) | {key.rsplit(":", 1)[-1] for key in set(web) | public}
            for port in used:
                endpoint = f"{host}:{port}"
                root = web.get(endpoint, {}).get("Handlers", {}).get("/", {})
                private = (
                    endpoint not in public
                    and tcp.get(port, {}).get("HTTPS") is True
                    and root.get("Proxy") == _TARGET
                )
                ports[int(port)] = ports.get(int(port), True) and private
    except (AttributeError, TypeError, ValueError):
        return None
    return ports


def _link(host: str, ports: dict[int, bool]) -> str | None:
    if ports.get(443):
        return f"https://{host}/"
    return next((f"https://{host}:{port}/" for port in sorted(ports) if ports[port]), None)


def phone_link() -> str | None:
    """Read the private HTTPS address of the running dashboard."""
    if (program := _program()) and (host := _hostname(program)):
        return _link(host, _ports(program, host) or {})
    return None


def _shell_program(program: str) -> str:
    if sys.platform == "win32":
        return "& '" + program.replace("'", "''") + "'"
    prefix = "TAILSCALE_BE_CLI=1 " if sys.platform == "darwin" else ""
    return prefix + shlex.quote(program)


def set_up() -> None:
    """Enable private phone access when the default port is free."""
    program = _program()
    if not program or not (host := _hostname(program)):
        print(
            "Install Tailscale on this computer and your phone from https://tailscale.com/download."
        )
        print("Sign in on both with the same account, then run tfs setup again.")
        return
    for attempt in range(2):
        ports = _ports(program, host)
        if ports is None:
            print("Could not read Tailscale Serve settings. Run tfs setup again.")
            return
        if url := _link(host, ports):
            print(f"Tailscale already serves TFS. Open this link on your phone: {url}")
            return
        if 443 in ports:
            port = next((n for n in range(8443, 65536) if n not in ports), None)
            print("Port 443 is in use. TFS kept its settings.")
            if port:
                print(
                    f"To use another private port, run: {_shell_program(program)} serve --bg --https={port} {_TARGET}"
                )
            return
        output = _run(program, "serve", "--bg", _TARGET)
        if output and output.startswith(_APPROVAL_PREFIX) and attempt == 0:
            print(f"Open this link to approve Tailscale Serve: {output}", flush=True)
            input("Press Enter after approval to try again. ")
            continue
        if output is not None and (url := _link(host, _ports(program, host) or {})):
            print(f"Open this link on your phone: {url}")
            return
        break
    print("TFS could not set up the phone link. Run tfs setup again.")
