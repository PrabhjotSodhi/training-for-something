"""AI tools that take TFS as a plain MCP server, by a command they run or a JSON file they read."""

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, eq=False)
class Tool:
    """One AI tool. It has a `command` that adds the server, or a JSON `file` under the home folder."""

    label: str
    command: str | None = None
    argv: tuple[str, ...] = ()
    list_argv: tuple[str, ...] = ("mcp", "list")
    file: str = ""
    key: str = "mcpServers"
    entry: dict = field(default_factory=dict)
    top: dict = field(default_factory=dict)


_ADD = ("mcp", "add", "tfs")
_RUN = ("{tfs}", "mcp")

TOOLS = (
    Tool("Cursor", file=".cursor/mcp.json", entry={"type": "stdio"}),
    Tool("OpenCode", "opencode", (*_ADD, "--global", "--", *_RUN)),
    Tool("Grok Build", "grok", (*_ADD, "--", *_RUN)),
    Tool("Google Antigravity CLI", "agy", file=".gemini/config/mcp_config.json"),
    Tool(
        "Kiro CLI",
        "kiro-cli",
        ("mcp", "add", "--name", "tfs", "--command", "{tfs}", "--args", "mcp", "--scope", "global"),
        ("mcp", "list", "global"),
    ),
    Tool("Pi", "pi", (*_ADD, "--", *_RUN)),
    Tool(
        "Meta Muse Code",
        "muse",
        file=".config/muse/settings.json",
        key="mcp_servers",
        entry={"transport": "stdio"},
        top={"schema_version": 1},
    ),
)


def find(tool: Tool, home: Path) -> str | None:
    """Return the tool's program, or its settings folder for a tool with no command. None if absent."""
    if tool.command:
        return shutil.which(tool.command)
    folder = home / Path(tool.file).parent
    return str(folder) if folder.is_dir() else None


def is_connected(
    tool: Tool, program: str, home: Path, run: Callable[[list[str]], tuple[bool, str]]
) -> bool:
    """Say whether the tool already has the TFS server: its list names tfs, or its file holds our entry."""
    if tool.command:
        ok, output = run([program, *tool.list_argv])
        return ok and re.search(r"\btfs\b", output) is not None
    try:
        servers = json.loads((home / tool.file).read_text(encoding="utf-8"))[tool.key]
        return servers["tfs"] == {**tool.entry, "command": _tfs(), "args": ["mcp"]}
    except (OSError, ValueError, LookupError, TypeError):
        return False


def _tfs() -> str:
    return shutil.which("tfs") or "tfs"


def add(
    tool: Tool, program: str, home: Path, run: Callable[[list[str]], tuple[bool, str]]
) -> str | None:
    """Add the TFS server to one tool. Return the problem, or None on success or when already added."""
    tfs = _tfs()
    if tool.command:
        ok, output = run([program, *(a.replace("{tfs}", tfs) for a in tool.argv)])
        lines = output.strip().splitlines()
        return None if ok else (lines[-1] if lines else "the command failed")
    path = home / tool.file
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else "{}"
        data = json.loads(text)
        servers = data.setdefault(tool.key, {})
        wanted = {**tool.entry, "command": tfs, "args": ["mcp"]}
        if servers.get("tfs") == wanted:
            return None
        if "tfs" in servers:
            return f"{path} already has a different tfs entry, {json.dumps(servers['tfs'])}. Left unchanged."
        servers["tfs"] = wanted
        for name, value in tool.top.items():
            data.setdefault(name, value)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".tfs-backup"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError) as error:
        return f"could not update {path}: {error}"
    return None
