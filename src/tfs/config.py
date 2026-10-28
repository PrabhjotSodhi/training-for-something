"""Where TFS keeps its data, and the ports it listens on."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

SCREEN_HOST = "127.0.0.1"
SCREEN_PORT = 47823
HOME_WIFI_PORT = 47824


def data_dir(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the TFS data folder. TFS_HOME overrides the OS default. Callers create it."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    if "TFS_HOME" in env:
        return Path(env["TFS_HOME"])
    if platform == "win32":
        base = Path(env["LOCALAPPDATA"]) if "LOCALAPPDATA" in env else home / "AppData" / "Local"
        return base / "TFS"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "TFS"
    base = Path(env["XDG_DATA_HOME"]) if "XDG_DATA_HOME" in env else home / ".local" / "share"
    return base / "tfs"


def db_path(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the path of the SQLite file inside the data folder."""
    return data_dir(env, platform, home) / "tfs.db"


def raw_dir(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the folder that holds raw files inside the data folder."""
    return data_dir(env, platform, home) / "raw"
