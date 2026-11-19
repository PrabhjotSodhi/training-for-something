# Training For Something

A free, open source coach for one person's goal. A small engine runs on your computer, reads your health and training apps, and builds numbers you can trust. The AI tool you already use reads those numbers and answers questions.

**Status:** in development. Data imports, questions and local plan/goal/limit preview and save tools are available. Sending plans to connected apps is still being built.

## Not medical advice

Training For Something gives no medical advice, diagnoses or doses, and never changes a clinician's targets. Its answers are not a substitute for a doctor or physio. If something feels like an emergency, call your local emergency number.

## How it works

- Your data is stored on your computer, in one folder. It is shared only with the apps you connect and the AI tool you choose.
- Plain code stores data, counts each workout once and builds daily totals.
- Your AI tool reads those numbers through the local ask TFS tool.

Read more in the [high level design](docs/HLD.md), the [code map](docs/code-map.md) and the [specs](docs/specs/).

## Ask about your data

You need Python 3.12 or later and Claude Code or Codex on the same computer. Until the installer arrives in the "Safe to live with" milestone, clone this repository and install from its folder, preferably in a virtual environment:

```sh
git clone https://github.com/PrabhjotSodhi/training-for-something.git
cd training-for-something
python -m pip install -e .
```

Both clients need `tfs` on PATH. Open a new terminal, activate the same virtual environment if you used one, and check:

```sh
tfs --version
```

Start your AI client from that terminal so it can find `tfs`. Import data first, for example with `tfs import /path/to/export.zip` for an Apple Health export. The tool reports a missing database until you import data or start TFS.

### Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add PrabhjotSodhi/training-for-something
/plugin install tfs@training-for-something
```

Enable the plugin and its local TFS MCP server when prompted. Start a new chat and ask, "What do my latest workouts show?"

### Codex

Copy the `plugins/tfs/skills/tfs` folder, including `SKILL.md` and its `references` folder, to your personal skills folder. [Current Codex documentation](https://developers.openai.com/codex/skills) uses `~/.agents/skills/tfs`. For installations using the older Codex skills location, copy it to `~/.codex/skills/tfs`, or `$CODEX_HOME/skills/tfs` if you set `CODEX_HOME`. Use the location supported by your installed version.

Register the local MCP server from a terminal where `tfs --version` works:

```sh
codex mcp add tfs -- tfs mcp
```

Restart Codex, select the TFS skill with `$tfs`, and ask about your data. If the skill is missing, check the skills folder for your Codex version. If the server cannot start, check that `tfs` is on PATH in the terminal that launches Codex.

## Plan changes in chat

Ask for a plan or a change. The coach reads your current plan version, calls
`propose` and shows the exact changes with their reason and sources. Preview
saves nothing. Approve `save` through your AI client's permission prompt to
store the proposal. To take back a saved change, ask for the opposite change,
such as removing an added session or moving a session back, and approve it like
any other change. Existing delivered edits use new queued updates, preserving
their original receipts. A removal can create a manual deletion task. An approved report records that you deleted the exact copy;
it is not provider verification. Restoring it creates a new copy after approval.
Provider writes remain disabled pending verification.

Keep `save` configured to ask for permission. An always-allow setting
can skip the prompt. TFS records the calling route but does not independently
prove consent; check the approval/refusal behavior in your installed client.
The installed AI CLI has your operating-system rights, and the SQL reader is
not an OS sandbox. A refused change must not be written through another route.

For example, ask: "Set a marathon goal for a future date, limit sessions to
60 minutes, and propose an easy 6 km run tomorrow." The tools can save that
local goal, limit and session together. They do not send it to a connected app
yet. Goal units are explicit; planned targets use metres and seconds when
writing, while the read views show km and minutes.

## Contributing

Work is planned as GitHub issues, grouped by milestone. Read [AGENTS.md](AGENTS.md) before you open a pull request.
