"""App tokens and API keys, kept in one private .env file in the data folder."""

import os
import tempfile
from pathlib import Path

from tfs import config


class PasswordStoreError(Exception):
    """The password store could not be used. The message never holds a secret."""


def get(name: str) -> str | None:
    """Return the secret stored under name, or None when there is none."""
    return _read_file().get(_stored_name(name))


def set(name: str, value: str) -> None:
    """Store a secret under name, replacing any earlier one."""
    if "\n" in value or "\r" in value:
        raise PasswordStoreError("A secret cannot contain a line break.")
    secrets = _read_file()
    secrets[_stored_name(name)] = value
    _write_file(secrets)


def delete(name: str) -> None:
    """Remove the secret stored under name. A name with no secret is not an error."""
    secrets = _read_file()
    if secrets.pop(_stored_name(name), None) is not None:
        _write_file(secrets)


def _stored_name(name: str) -> str:
    return name.upper().replace(":", "_")


def _secrets_path() -> Path:
    return config.data_dir() / ".env"


def _read_file() -> dict[str, str]:
    path = _secrets_path()
    # Raised after the except block: a UnicodeDecodeError holds every byte of the file.
    try:
        lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
    except FileNotFoundError:
        return {}
    except ValueError:
        lines = None
    if lines is None or not all("=" in line for line in lines):
        raise PasswordStoreError(
            f"The password file {path} is damaged. Delete it and connect your apps again."
        )
    return dict(line.split("=", 1) for line in lines)


def _write_file(secrets: dict[str, str]) -> None:
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Every write gets its own file, so two writers never touch each other's data.
    # mkstemp creates the file private, so there is no gap before a mode change.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in secrets.items())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
