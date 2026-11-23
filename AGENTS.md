# AGENTS.md

Rules for every coding agent and contributor in this repo. Read this file, then the issue you are working on.

Training For Something (TFS) is a free, open source, local-first coach. A small Python engine on the user's computer reads health and training data into one SQLite file. The user's own AI tool reads it through a guarded, read-only tool and proposes changes the user approves.

## Read before you write code

1. The issue: its outcome, acceptance examples, expected files and "done when" command.
2. `docs/code-map.md`: where every file goes, the database tables and the 8 module rules.
3. The HLD and spec sections the issue links to, in `docs/HLD.md` and `docs/specs/`.

## Commands

```bash
uv sync                                               # once, and after pyproject.toml changes
uv run ruff check . && uv run ruff format --check .   # lint and format, as CI runs them
uv run python -m unittest discover -s tests -t .      # all tests
uv run python -m unittest tests.test_raw_store        # one file
npm --prefix dashboard install                        # once, and after dashboard/package.json changes
npm --prefix dashboard run build                      # build the dashboard into src/tfs/web/dashboard/
```

Python 3.12. uv installs everything, and `uv.lock` pins every version. Add a package with `uv add`, never with pip. Tests use `unittest`. Hypothesis is allowed in tests only. Ruff lints and formats. CI fails on any Ruff finding.

## Rules

1. Runtime packages are the ones listed in `pyproject.toml`. Add one only when the issue names it. A package qualifies when it solves a problem many projects share, it is widely used and maintained, and it installs with TFS with no second program to run.
2. One issue per pull request. Touch only the files the issue lists under "Expected files", plus their tests. If you need another file or table, stop and ask on the issue.
3. Never edit `docs/HLD.md`, `docs/code-map.md`, `docs/specs/`, `AGENTS.md`, `ruff.toml` or `.github/` unless the issue says so. Never add `# noqa` to silence Ruff. If a finding looks wrong, stop and ask on the issue.
4. Follow the issue's test policy. "Test first" means you commit the failing tests from the acceptance examples before the code that makes them pass.
5. Never delete, skip or weaken a test to make it pass. If a test looks wrong, stop and ask on the issue.
6. Tests use invented people and invented data only. Never commit real health data, real exports or real API responses.
7. No module outside `connectors/<name>/` branches on an app's name.
8. Secrets never appear in logs, errors, raw files, test output or commits.
9. Times are stored as UTC ISO 8601 strings ending in `Z`, plus a UTC offset in minutes and a local date where the HLD says so. Use timezone-aware `datetime` only.
10. Every new public function has type hints and one short docstring.

## Git

1. Branch from current `main`, named `agent/<issue number>-<short-name>`.
2. Commit messages say what changed and why, in plain English. No "Generated with" or co-author lines.
3. Open one pull request using the template. Link the issue with `Closes #<number>`.
4. Never push to `main`, force push, rebase a shared branch, merge, tag or release. The maintainer merges every pull request.

## Releasing

The maintainer cuts each release by hand when a milestone is done. Versions follow PEP 440: `0.1.0a1`, `0.1.0a2` and so on until `0.1.0`, the public beta. After that, the last digit goes up for fixes and the middle digit for new features.

1. Run the tests and Ruff the way `.github/workflows/tests.yml` does.
2. Set `__version__` in `src/tfs/__init__.py`.
3. Give the top CHANGELOG section the version and date, and list the known gaps.
4. Merge the pull request, tag the merge commit `v<version>` and publish a GitHub release with that CHANGELOG section. Mark alphas as pre-releases.

## When to stop

Stop, label the issue `agent:blocked`, and comment with the command you ran, the failure, and what you think causes it, when:

1. You have made 3 different attempts and the "done when" command still fails.
2. The same failure appears twice in a row.
3. The issue conflicts with the HLD, the code map or a spec.
4. The work needs a new dependency, file, table or design decision.

## Writing

Docs, comments, issue comments and pull request text use plain English: short sentences, active voice, no jargon where a plain word works. Comments explain why, not what. Comment only traps and invisible constraints.
