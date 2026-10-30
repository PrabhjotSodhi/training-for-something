"""Runs one connector operation in a short-lived child process with a deadline.

Raw data moves by file path. The child returns only a small JSON result on stdout.
"""

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Iterator
from pathlib import Path

_OPS = ("fetch", "read", "send", "connect")
_NAME = re.compile(r"[a-z0-9_]+")
_NOT_CONNECTORS = ("core", "testing")
_CONTRACT_PATH = Path(__file__).with_name("contract.py")
_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_STDERR_LINES = 20


class RunnerTimeoutError(Exception):
    """The connector did not finish before its deadline and was killed."""


class RunnerFailedError(Exception):
    """The connector process crashed or returned something that is not a JSON object."""


class ConnectorUserError(Exception):
    """The connector refused the request and says what the user should fix."""


def run(
    connector: str, op: str, payload: dict, *, timeout_s: float, module: str | None = None
) -> dict:
    """Run one connector operation in a child process and return its JSON result."""
    _check_name(connector)
    if op not in _OPS:
        raise ValueError(f"op must be one of {', '.join(_OPS)}, got {op!r}")
    module = module or f"tfs.connectors.{connector}.connector"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(_SOURCE_ROOT), str(Path.cwd()))),
        "PYTHONIOENCODING": "utf-8",
    }
    with (
        tempfile.TemporaryDirectory() as workdir,
        subprocess.Popen(
            [sys.executable, "-m", "tfs.connectors.core.runner", module, op],
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child,
    ):
        try:
            out, err = child.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise RunnerTimeoutError(
                f"{connector} {op} did not finish within {timeout_s} seconds"
            ) from None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    hide = _Hider(payload)
    tail = "\n".join(hide(err.decode("utf-8", "replace")).splitlines()[-_STDERR_LINES:])
    if child.returncode != 0:
        raise RunnerFailedError(f"{connector} {op} exited with code {child.returncode}:\n{tail}")
    try:
        result = json.loads(out)
    except (ValueError, RecursionError):
        result = None
    if not isinstance(result, dict):
        raise RunnerFailedError(f"{connector} {op} did not return a JSON object:\n{tail}")
    if "user_error" in result:
        raise ConnectorUserError(hide(str(result["user_error"])))
    return result


def http_get(url: str, headers: dict, timeout_s: float = 30) -> tuple[int, dict, bytes]:
    """GET an https URL and return status, headers and body. 4xx and 5xx are returned, not raised."""
    _require_https(url)
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout_s) as reply:
            return reply.status, dict(reply.headers), reply.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read()


def reader_version(connector: str | None = None, *, folder: Path | None = None) -> str:
    """Return 16 hex characters that change when a connector's code or the contract changes."""
    if folder is None:
        if connector is None:
            raise ValueError("give a connector name or a folder")
        _check_name(connector)
        folder = Path(__file__).resolve().parent.parent / connector
    if not folder.is_dir():
        raise ValueError(f"connector folder not found: {folder}")
    files = sorted(
        (path.relative_to(folder).as_posix(), path)
        for path in folder.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "manifest.json")
    )
    digest = hashlib.sha256()
    for relative, path in files:
        digest.update(f"{relative}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    digest.update(_CONTRACT_PATH.read_bytes())
    return digest.hexdigest()[:16]


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    # A redirect to http:// or file:// would send the request headers, which hold secrets, there.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Hider:
    def __init__(self, payload: dict) -> None:
        found = {*_strings(payload.get("secrets")), *_strings(payload.get("answers"))}
        found |= {repr(secret)[1:-1] for secret in found}
        self.secrets = sorted(found - {""}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[secret]")
        return text


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _check_name(connector: str) -> None:
    if not _NAME.fullmatch(connector) or connector in _NOT_CONNECTORS:
        raise ValueError(f"connector must be lowercase letters, digits and _, got {connector!r}")


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise ValueError("Only https:// addresses can be fetched.")


def _child(module_name: str, op: str) -> None:
    # Windows would turn each \n into \r\n, and a multi-line secret would no longer match.
    sys.stderr.reconfigure(newline="\n")
    payload = json.loads(sys.stdin.buffer.read())
    function = getattr(importlib.import_module(module_name), op, None)
    if function is None:
        result = {"unsupported": True}
    else:
        extra = {"http": http_get} if op == "fetch" else {}
        try:
            result = function(**payload, **extra)
        except ValueError as error:
            result = {"user_error": str(error)}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2])
