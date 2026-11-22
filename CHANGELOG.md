# Changelog

Versions follow Python's PEP 440. Each release starts with "Breaking changes". Each breaking change says what to use instead. Changes to `coach_v1` views and the ask TFS tool come first.

## Unreleased

### Breaking changes

- The `preview_undo` and `undo` tools are gone. To take back a saved change, approve a new change that does the opposite, such as `remove_session` for an added session or `move_session` back to the old date.
- The `report_delivery_deleted` action is gone. When a removed session cannot be deleted in an app, TFS gives the person a task to delete it there and forgets the link. TFS does not check the app. The `resolved_by_version` and `resolution` columns are gone from `coach_v1_deliveries`. Read `manual_action=delete` for the open task.

### Changed

- Install with `uv tool install git+https://github.com/PrabhjotSodhi/training-for-something`. It puts `tfs` on PATH with no virtual environment to activate. Contributors run `uv sync`, and `uv.lock` pins every version.

## 0.1.0a1, 2026-11-19

The first alpha of 0.1.0, the first public version. Expect bugs and breaking changes before 0.1.0.

### Breaking changes

- The ask TFS tool runs on the official MCP Python SDK. The tool schemas are generated, so their JSON layout differs. Tool names, arguments and results do not change. A failed call returns plain text that starts with `Error executing tool`, where it returned a JSON object with an `error` key.
- App keys moved to a `.env` file in the data folder. Run `tfs connect` again for each app.

### Added

- Your data in: import an Apple Health export, receive Health Auto Export pushes from your phone and fetch Hevy workouts. A workout recorded by two apps counts once. Daily steps, energy, sleep, resting heart rate and weight count each moment once.
- Commands: `tfs start`, `tfs connect`, `tfs import`, `tfs sync`, `tfs status` and `tfs mcp`.
- A crash during an import, push or sync loses nothing and stores nothing twice. Rebuilding from saved files gives the same data as live imports.
- Ask your coach: the TFS plugin for Claude Code and the TFS skill for Codex. Your AI tool reads workouts, sets, laps, daily totals, goals and plans through the local ask TFS tool and a read-only SQL reader.
- Plans: save goals, training limits and planned sessions from your AI chat, with a preview before each save and undo after. Imported workouts are matched to planned sessions.
- Design documents: the HLD, the code map and the plan items, proposal actions and metrics specs.

### Known gaps

- Sending plans to Hevy is built but turned off until it passes a check on a real account (#185).
- Not yet checked on real devices: a full Apple Health export (#26), Health Auto Export through Tailscale Serve with the phone locked (#24), a phone reaching the computer by name on Windows and Linux (#25), the plugin in Claude Code and Codex (#27), and a yes in the AI tool's permission prompt saving a change (#28).
- The home Wi-Fi listener has no limit on total request time or requests in progress (#69).
