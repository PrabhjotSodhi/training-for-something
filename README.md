<div align="center">

<img src="docs/images/banner.webp" alt="Training For Something: the coach for your goal" width="880">

**A free, open source coach for your goal.**

It runs on your computer, reads your health and training apps,<br>
and answers your questions through the AI tool you already use.

[![tests](https://github.com/PrabhjotSodhi/training-for-something/actions/workflows/tests.yml/badge.svg)](https://github.com/PrabhjotSodhi/training-for-something/actions/workflows/tests.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Release](https://img.shields.io/github/v/release/PrabhjotSodhi/training-for-something?include_prereleases)](https://github.com/PrabhjotSodhi/training-for-something/releases)

[Install](#install) · [Releases](https://github.com/PrabhjotSodhi/training-for-something/releases) · [License](#license)

</div>

## Know where you stand every morning

- Fall behind and your coach adjusts the plan. You ask in chat and approve the change.
- A recovery score from your HRV, resting heart rate and sleep, against your own normal.
- How much sleep you need tonight. `Coming soon` Today the sleep card shows your nights against a target.
- Food from a photo, with protein, carbs and fat. `Coming soon`
- Lab results over time. `Coming soon`

## Features

<table>
<tr>
<td width="50%" valign="top"><img src="docs/images/feature-goal.webp" alt="The goal plate with a forecast range of 0:48 to 0:50"><br><b>Know where you stand</b><br>See your forecast for your goal and your week at a glance. The dashboard runs on your computer.</td>
<td width="50%" valign="top"><img src="docs/images/feature-ask.webp" alt="A question about long runs answered with the person's own numbers"><br><b>Ask anything</b><br>Ask in your AI tool. Answers come from your own data and the research.</td>
</tr>
<tr>
<td width="50%" valign="top"><img src="docs/images/feature-approve.webp" alt="A prompt asking to save one added easy run, with Yes and No"><br><b>A plan that updates</b><br>Say yes and your coach saves the change. Nothing changes without your yes.</td>
<td width="50%" valign="top"><img src="docs/images/feature-hevy.webp" alt="An upper body routine marked as sent to Hevy"><br><b>Your plan in Hevy</b><br>Today's strength session waits for you in Hevy. Sending plans to Garmin <code>Coming soon</code></td>
</tr>
<tr>
<td width="50%" valign="top"><b>Train the right muscles</b><br>See your sets by muscle each week. Ask your coach how your lean mass and waist are changing.</td>
<td width="50%" valign="top"><img src="docs/images/feature-apps.webp" alt="Apple Health, Hevy, Health Auto Export, Claude Code and Codex"><br><b>Runs on the AI subscription you already pay for</b><br>Pick from 9 AI tools, including Claude Code and Codex.</td>
</tr>
<tr>
<td width="50%" valign="top"><img src="docs/images/feature-morning.webp" alt="A morning note that changes today's intervals to an easy run"><br><b>A coach in the background</b> <code>Coming soon</code><br>It checks your data each morning, so you do not have to ask.</td>
<td width="50%" valign="top"><img src="docs/images/feature-effort.webp" alt="Effort up 12 percent this week against the normal"><br><b>Know how hard your week was</b><br>Your workout ratings against your own normal.</td>
</tr>
</table>

See the [roadmap](ROADMAP.md) for what comes after 0.1.0.

## Install

You need one of the AI tools listed under Works with. One line installs TFS and starts setup.

Mac and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/PrabhjotSodhi/training-for-something/main/install.sh | sh
```

Windows, in PowerShell:

```powershell
curl.exe -fsSLo install.ps1 https://raw.githubusercontent.com/PrabhjotSodhi/training-for-something/main/install.ps1; powershell -ExecutionPolicy ByPass -File install.ps1; del install.ps1
```

Setup connects your AI tools and your apps, then opens the dashboard. Run `tfs start --open` any time to open it again. Run `tfs update` to update.

## Works with

| AI tools | Apps |
|---|---|
| Claude Code (tested) | Apple Health export |
| Codex (tested) | Health Auto Export |
| Cursor (should work) | Hevy |
| OpenCode (should work) | Garmin (read only, with a Garmin Connect Personal API key) |
| Grok Build (should work) | |
| Google Antigravity CLI (should work) | |
| Kiro CLI (should work) | |
| Pi (should work) | |
| Meta Muse Code (should work) | |

Windows and macOS are tested. Linux should work but is untested.

TFS works next to other MCP servers in the same AI tool. Strava runs an [official MCP server](https://support.strava.com/en-us/articles/15401531-what-is-the-strava-mcp-connector) for subscribers.

## How it works

```mermaid
flowchart LR
  A[Your apps] --> B[TFS on your computer] --> C[Your AI tool]
```

Plain code stores your data, counts each workout once and builds daily totals. The same workout from two apps counts once. Your AI tool reads those numbers through the local TFS tool. Your data goes only to the apps you connect and the AI tool you choose.

Read more in the [high level design](docs/HLD.md), the [code map](docs/code-map.md) and the [specs](docs/specs/).

## Plan changes in chat

Ask for a change, like "Limit my sessions to 45 minutes and add an easy run tomorrow." TFS shows the exact changes and the reason for each. Nothing saves until you approve it in your AI tool's permission prompt. Approved strength sessions go to Hevy, into a routine you pick or a new one.

Keep the `save` tool set to ask for permission. An always allow setting skips that prompt. Your AI tool runs with your computer's permissions, so TFS cannot stop it from writing a change you refused through another route.

## Go back to an older version

Before an update changes your database, TFS saves a copy in the `backups` folder of the TFS data folder. It keeps the 2 newest. To go back:

1. Stop TFS with Ctrl+C.
2. Install the older release: `uv tool install --force <wheel URL>`
3. Copy the backup you want over `tfs.db` in the data folder.

Data saved after the update is lost.

## Not medical advice

Training For Something gives no medical advice, diagnoses or doses, and never changes a clinician's targets. Its answers are not a substitute for a doctor or physio. If something feels like an emergency, call your local emergency number.

## Set up by hand

Skip this if you ran `tfs setup`.

1. Install the wheel from the newest [release](https://github.com/PrabhjotSodhi/training-for-something/releases): `uv tool install --force <wheel URL>`
2. Add the plugin to Claude Code:
   ```sh
   claude plugin marketplace add PrabhjotSodhi/training-for-something
   claude plugin install tfs@training-for-something
   ```
3. Or add it to Codex:
   ```sh
   codex plugin marketplace add PrabhjotSodhi/training-for-something
   codex plugin add tfs@training-for-something
   ```
4. Import your data, for example `tfs import export.zip` for an Apple Health export.
5. Run `tfs start --open` to open the dashboard.

If your terminal cannot find `tfs`, run `uv tool update-shell` and open a new terminal.

## From source

```sh
npm --prefix dashboard install && npm --prefix dashboard run build && uv tool install --editable .
```

## Contributing

Work is planned as GitHub issues, grouped by milestone. Read [AGENTS.md](AGENTS.md) before you open a pull request.

## License

TFS is free software under the [GNU Affero General Public License v3.0 or later](LICENSE). You may use, change and share it. If you share a changed version, or run one as a service for others, you must publish your source code under the same license.

The license covers the code only. It gives no right to use the Training For Something name or logo.
