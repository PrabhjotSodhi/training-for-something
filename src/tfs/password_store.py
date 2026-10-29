"""App tokens and API keys, kept in the OS password store or a user-only file."""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import keyring
import keyring.backends.fail
import keyring.errors

from tfs import config

SERVICE_NAME = "training-for-something"
STORE_UNAVAILABLE = "The OS password store is locked or unavailable."


class PasswordStoreError(Exception):
    """The password store could not be used. The message never holds a secret."""


def get(name: str) -> str | None:
    """Return the secret stored under name, or None when there is none."""
    if _use_file():
        return _read_file().get(name)
    return _ask_os_store(keyring.get_password, SERVICE_NAME, name)


def set(name: str, value: str) -> None:
    """Store a secret under name, replacing any earlier one."""
    if _use_file():
        secrets = _read_file()
        secrets[name] = value
        _write_file(secrets)
    else:
        _ask_os_store(keyring.set_password, SERVICE_NAME, name, value)


def delete(name: str) -> None:
    """Remove the secret stored under name. A name with no secret is not an error."""
    if _use_file():
        secrets = _read_file()
        if secrets.pop(name, None) is not None:
            _write_file(secrets)
    elif _ask_os_store(keyring.get_password, SERVICE_NAME, name) is not None:
        _ask_os_store(keyring.delete_password, SERVICE_NAME, name)


def _use_file() -> bool:
    if os.environ.get("TFS_PASSWORD_STORE") == "file":
        return True
    if not isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring):
        return False
    if sys.platform == "win32":
        raise PasswordStoreError("Windows has no OS password store that TFS can use.")
    return True


def _ask_os_store(operation: Callable[..., Any], *arguments: str) -> Any:
    # The keyring error is dropped on purpose: backends may echo what they were given.
    try:
        return operation(*arguments)
    except keyring.errors.KeyringError:
        raise PasswordStoreError(STORE_UNAVAILABLE) from None


def _secrets_path() -> Path:
    return config.data_dir() / "secrets.json"


def _read_file() -> dict[str, str]:
    path = _secrets_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        secrets = json.loads(text)
    except json.JSONDecodeError:
        secrets = None
    if not isinstance(secrets, dict) or not all(
        isinstance(value, str) for value in secrets.values()
    ):
        raise PasswordStoreError(
            f"The password file {path} is damaged. Delete it and connect your apps again."
        )
    return secrets


def _write_file(secrets: dict[str, str]) -> None:
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        # The file is created private. Writing then changing the mode would leave a gap.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(secrets, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
