# Code map

Where each part of the high level design (`docs/HLD.md`) lives in code, the database tables, and how the parts call each other. Every ticket names files from this map. A ticket that needs a file or table not listed here stops and asks.

Python 3.12, standard library only. `keyring` is the one approved runtime package. Hypothesis is a test-only package.

## 1. Folders

```text
training-for-something/
  AGENTS.md                   rules for every coding agent
  README.md
  CHANGELOG.md
  pyproject.toml              package metadata, the `tfs` command
  docs/                       HLD, specs, this map
  src/tfs/
    __main__.py               `python -m tfs` runs the CLI
    engine.py                 the background process: starts the web server, scheduler and job worker
    config.py                 where the data folder is, per OS
    cli/                      one file per command: start, connect, import_, sync, status, mcp, query
    persistence/
      sqlite.py               opens tfs.db with the right settings, one place only
      migrations.py           runs numbered migrations, reads and sets user_version
      sql/0001_init.sql       one numbered SQL file per migration
    importer/
      raw_store.py            saves raw files safely, registers them, scans raw/ on start
      records.py              stores what a reader returns: new versions replace old ones, deletes apply
      session.py              one import run: save the file, read it, store records, rebuild the days it touched
    connectors/
      core/
        contract.py           the record and result shapes every connector returns
        runner.py             runs one connector operation in a short-lived process with a deadline
        send.py               runs a send helper and validates/redacts its outcome
      testing/
        contract_checks.py    the 5 checks every connector must pass
      apple_health/           one folder per connector
        manifest.json
        connector.py          fetch(), read(), send(), connect() as the app supports
      health_auto_export/
      hevy/
        routines.py           Hevy-only routine mapping/readback; writes stay disabled pending proof
    duplicates/
      workouts.py             links the same workout from two apps into one session group
      review.py               the "Possible duplicates" screen: your links and unlinks
    source_priority/
      priority.py             which app's number shows, from your source list
      daily_totals.py         one app's total per day and data type
    scores/                   readiness.py, load.py, recovery.py, zones.py, fitness_age.py, forecasts.py
    mcp/
      views.sql               the coach_v1_* views
      tool_access.py          the guarded read-only reader
      server.py               the ask TFS tool over stdio
      proposal_tools.py       schemas and dispatch for propose, save and undo
    providers/
      briefing.py             the briefing that starts every run TFS starts
      claude_code.py          one file per AI tool
      codex.py
    proposals/
      actions.py              validates proposals and calculates resulting state
      targets.py              validates and stores goals and training limits
      apply.py                validates a proposal, applies it, undoes it
      journal.py              the append-only journal
    scheduling/
      scheduler.py            the 60 second loop, next_due
      jobs.py                 atomic queue, unsent undo, claim, finish and crash reconciliation
      triggers.py             the missed-session rule
    web/
      server.py               HTTP server on 127.0.0.1, sessions, request checks
      push.py                 the push endpoint for phone apps
      views/                  one file per screen
      templates/
      static/
    diagnostics.py            one status report for screens and `tfs status`
    password_store.py         app tokens and API keys in the OS password store, with a user-only file fallback
    backup_restore.py         backup and restore
    export.py                 export your data
    delete_data.py            Delete my data
  plugins/
    claude-code/              the TFS plugin: skill plus ask TFS tool config
    codex/                    the TFS skill for Codex
  tests/
    fixtures/<connector>/     invented files shaped like each app's real ones
    test_<module>.py          one test file per module
```

### Naming a new file or folder

A name passes when:

1. A non-coder can guess what is inside from the folder and file name together.
2. It uses a word users already see, in TFS or in apps like iPhone Health, or a word the HLD uses.
3. It does not clash with another folder or table.
4. It covers everything inside.

Module names are short and lowercase, as PEP 8 asks. The folder carries the context: `duplicates/workouts.py`, never `duplicate_workout_linker.py`.

## 2. Database tables

Each milestone adds one numbered migration for the tables it needs. `0001_init.sql` creates the 0.1 tables: `files`, `sources`, `source_records`, `source_deletes`, `session_parts`, `session_groups`, `session_group_members`, `day_facts` and `rebuild_dates`. `0002_plans.sql` adds `journal`, `goals`, `limits` and `plan_items`, and lets `session_parts` belong to a plan item. A merged migration is never edited. Later changes are new numbered files.

| Table | Kind | Holds |
|---|---|---|
| `files` | Imported | One row per raw file: hash, arrival order, connector, format, received time, reader hash, status (new, reading, ok, partial, unreadable, needs AI, failed, missing), message |
| `sources` | Sync state | One row per connector: enabled, cursor, last attempt, last contact, last received, last error. "Data covered until" is computed from `source_records`. |
| `source_records` | Imported | One row per record a reader returned: id, file hash, kind, stream, key, revision, start in UTC, UTC offset, local date, family, label, values JSON with units, origin app, origin device, plan reference, deleted time |
| `source_deletes` | Imported | Deletes an app reported, kept even when the record has not arrived yet, so an older file read late cannot bring it back |
| `session_parts` | Imported or yours | Laps, sets, rounds and splits, owned by exactly one source record or plan item |
| `session_groups` | Derived | Records linked as one workout, with the chosen value per field and its source |
| `session_group_members` | Derived | Which source records belong to which group |
| `rebuild_dates` | Work | Dates whose groups and day facts must be rebuilt, saved with the records that changed them and cleared by the rebuild |
| `day_facts` | Derived | One row per day, metric and chosen source: value, coverage, formula version |
| `scores` | Derived | Readiness, load, recovery and forecasts per day, with inputs, range and formula version |
| `journal` | Yours | Append-only. One row per saved proposal or user action: actions JSON, who approved, time, format version |
| `goals`, `limits` | Yours | Current goals and limits, kept in step with the journal |
| `plan_items` | Yours | Current plan, as in the plan items spec |
| `remote_links` | Yours | Plan item, connector, remote id, remote version |
| `source_order` | Yours | Your source list per data type |
| `chat` | Yours | Visible chat turns |
| `jobs` | Work | Approved send payloads with stable op id, item/journal version, status, next due, attempts and result; AI/trigger jobs follow later |

"Derived" tables can be dropped and rebuilt from `source_records` and the journal. "Yours" tables are kept by backups.

## 3. How the parts call each other

```text
connector (in connectors/core/runner.py)  →  importer.raw_store.save()  →  files row
importer  →  connector read() in runner.py  →  importer.records  →  source_records (insert or replace by revision)
source_records  →  duplicates.workouts.update(dates)  →  session_groups
duplicates  →  source_priority.daily_totals.rebuild(dates)  →  day_facts  →  scores
coach views  ←  mcp/tool_access.py  ←  mcp/server.py  ←  the AI tool
AI proposal  →  proposals.apply()  →  journal + yours tables + send jobs, one transaction
scheduling/jobs.py  →  connectors/core/send.py  →  connector send() in runner.py  →  remote_links
```

## 4. Rules every module follows

1. Only `persistence/sqlite.py` opens the database. It sets WAL, `synchronous=FULL` and a busy timeout for `tfs.db`. It also opens the empty `tfs.db.save-priority` coordination file in rollback mode; this file holds locks only and must not be unlinked during operation.
2. The Save path's connection never runs a checkpoint. Imports run checkpoints.
3. Imports commit about 500 rows per transaction and pause before each batch and monthly rebuild while a Save or undo holds its intent lock.
4. No module branches on an app's name. App knowledge lives in `connectors/<name>/`.
5. Connectors run only through `connectors/core/runner.py`, never imported by the engine.
6. The AI reads only through `mcp/tool_access.py`.
7. Every change to your data goes through `proposals.apply()`.
8. Raw files are never edited or deleted, except by Delete my data.

## 5. What each milestone builds

| Version | Builds |
|---|---|
| 0.1 Your data in | `config`, `persistence/`, `importer/`, `connectors/core/` (contract, runner), `connectors/testing/`, `apple_health`, `health_auto_export`, `hevy` (read), `duplicates/workouts`, `source_priority/` (day facts for steps, active energy, sleep, resting heart rate and weight. HRV records are stored from 0.1, and day facts for HRV, total energy, body fat and VO2 max follow in 0.4 with scores), a minimal `engine` with `web/server.py` serving only `web/push.py` (one instance, push endpoint over Tailscale or home Wi-Fi), `cli/` (`start`, `connect`, `import`, `sync`, `status`), `diagnostics`, `password_store` |
| 0.2 Ask your coach | `mcp/`, `providers/briefing.py`, `plugins/`, `cli/mcp` |
| 0.3 Plans | `proposals/`, `scheduling/jobs.py`, `connectors/core/send.py`, Hevy `send`, `goals`, `plan_items`, `remote_links` |
| 0.4 Every morning | `scores/`, `scheduling/scheduler.py`, `scheduling/triggers.py`, food and document actions, `providers/` for runs TFS starts |
| 0.5 Dashboard and phone | The rest of `engine` and `web/`: screens, sign-in, phone pairing, `duplicates/review.py` |
| 0.6 More apps | `connectors/<name>/` for the rest |
| 0.7 Safe to live with | `backup_restore`, `export`, `delete_data`, the upgrade path (backup, then migrate), install and onboarding |
