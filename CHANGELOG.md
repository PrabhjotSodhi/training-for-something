# Changelog

Versions follow Python's PEP 440. Each release starts with "Breaking changes". Each breaking change says what to use instead. Changes to `coach_v1` views and the ask TFS tool come first.

## Unreleased

### Added

- `tfs update` installs the newest release after checking its SHA-256, then starts TFS again if it was running (#287). On Windows it waits until your AI tools are closed, because a reinstall breaks a running `tfs mcp` there. An AI tool left open on Mac or Linux asks you to restart it.
- `tfs start` checks GitHub for a newer release at start and every 24 hours. The terminal says "TFS {version} is ready. Run tfs update." once per version, and the dashboard shows it with a copy button. Set `TFS_NO_UPDATE_CHECK=1` to turn the check off.
- Before an update changes your database, TFS saves a copy in the `backups` folder of the data folder and keeps the 2 newest (#288). `tfs start` prints the path.
- `coach_v1_app_targets` lists your Hevy routines after each sync, and `coach_v1_plan_items` has `send_into_json`.
- `add_session` and `change_session` take `send_into` to send a strength session into a Hevy routine you pick. The preview shows its title in `send_into_titles`.
- Approved strength sessions are sent to Hevy (#185). An update keeps the routine's title and folder. Removing a session sent into a routine you picked leaves the routine as it is.

## 0.1.0a3, 2026-11-24

The third alpha of 0.1.0. It adds guided setup and a one-line installer. Expect bugs and breaking changes before 0.1.0.

### Added

- One-line install for Mac, Linux and Windows. It installs uv if you do not have it, installs the newest TFS release, then runs `tfs setup`. Run the same line again to update.
- `tfs setup` connects TFS to Claude Code and Codex, then walks you through each app you pick. For Apple Health export you drag the file onto the window. For Hevy you paste your API key. For Health Auto Export you scan one QR code, and your phone opens a page with a button for each automation.
- `tfs start` turns on the home Wi-Fi listener when an app that sends from your phone is connected, so you no longer need `--home-wifi`.

## 0.1.0a2, 2026-11-24

The second alpha of 0.1.0. It adds the dashboard and the morning scores. Expect bugs and breaking changes before 0.1.0.

### Added

- Dashboard: `tfs start` serves the Overview on this computer. It shows your goal plate, this week's sessions, the last 7 nights of sleep, and trend cards for resting heart rate, HRV, weight, VO2 max and training load against your own normal range. Your AI tool saves the layout, so changing it needs no rebuild.
- Scores: readiness, and training load per day from strength, cardio and effort. Effort load reads workout effort from Health Auto Export.
- Day facts for HRV, body fat and VO2 max.
- Your AI tool can save a forecast for each goal, and goals take any type, such as a mountain climb.
- A 60 second scheduler. The morning rules it will run come next (#211, #212).

### Breaking changes

- The `preview_undo` and `undo` tools are gone. To take back a saved change, approve a new change that does the opposite, such as `remove_session` for an added session or `move_session` back to the old date.
- The `report_delivery_deleted` action is gone. When a removed session cannot be deleted in an app, TFS gives the person a task to delete it there and forgets the link. TFS does not check the app. The `resolved_by_version` and `resolution` columns are gone from `coach_v1_deliveries`. Read `manual_action=delete` for the open task.

### Changed

- Install with `uv tool install <wheel URL>`, using the command on the release page. The wheel includes the built dashboard, so you need no Node and no git. It puts `tfs` on PATH with no virtual environment to activate. CI builds each release wheel and attaches it to the release. Contributors run `uv sync`, and `uv.lock` pins every version.

### Known gaps

- Sending plans to Hevy is built but turned off until it passes a check on a real account (#185).
- Effort load does not yet read the Apple Health export (#208).
- Not yet checked on real devices: a full Apple Health export (#26), Health Auto Export through Tailscale Serve with the phone locked (#24), a phone reaching the computer by name on Windows and Linux (#25), the plugin in Claude Code and Codex (#27), a yes in the AI tool's permission prompt saving a change (#28), and background AI runs on a Mac (#30).
- The home Wi-Fi listener has no limit on total request time or requests in progress (#69).

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
