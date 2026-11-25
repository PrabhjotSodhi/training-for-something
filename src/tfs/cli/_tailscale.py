"""Find private phone links and set up Tailscale Serve."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from tfs import config

_TARGET = f"http://{config.SCREEN_HOST}:{config.SCREEN_PORT}"
_INSTALL_PATHS = {
    "win32": Path("C:/Program Files/Tailscale/tailscale.exe"),
    "darwin": Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
}


def _run(*args: str) -> str | None:
    program = shutil.which("tailscale")
    if not program:
        path = _INSTALL_PATHS.get(sys.platform)
        if not path or not path.is_file():
            return None
        program = str(path)
    try:
        if args[-1] == "--json":
            result = subprocess.run(
                [program, *args],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            return result.stdout if result.returncode == 0 else None
        with subprocess.Popen(
            [program, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
        ) as process:
            try:
                for line in process.stdout:
                    # The CLI can wait indefinitely after printing its approval link.
                    if line.strip().startswith("https://login.tailscale.com/"):
                        return line.strip()
                return "" if process.wait() == 0 else None
            finally:
                if process.poll() is None:
                    process.kill()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _read(*args: str) -> dict | None:
    try:
        data = json.loads(_run(*args, "--json") or "")
    except ValueError:
        return None
    # Tailscale can return JSON null when no Serve config exists.
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def _hostname() -> str:
    status = _read("status") or {}
    if status.get("BackendState") == "Running":
        name = (status.get("Self") or {}).get("DNSName")
        if isinstance(name, str):
            return name.rstrip(".")
    return ""


def _ports(host: str) -> dict[int, bool] | None:
    """Map occupied ports to whether they privately serve the dashboard."""
    status = _read("serve", "status")
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
    host = _hostname()
    return _link(host, _ports(host) or {}) if host else None


def set_up() -> None:
    """Enable private phone access when the default port is free."""
    host = _hostname()
    if not host:
        print(
            "Install Tailscale on this computer and your phone from https://tailscale.com/download."
        )
        print("Sign in on both with the same account, then run tfs setup again.")
        return
    for attempt in range(2):
        ports = _ports(host)
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
                    f"To use another private port, run: tailscale serve --bg --https={port} {_TARGET}"
                )
            return
        approval = _run("serve", "--bg", _TARGET)
        if approval and attempt == 0:
            print(f"Open this link to approve Tailscale Serve: {approval}", flush=True)
            input("Press Enter after approval to try again. ")
            continue
        if approval == "" and (url := _link(host, _ports(host) or {})):
            print(f"Open this link on your phone: {url}")
            return
        break
    print("TFS could not set up the phone link. Run tfs setup again.")
