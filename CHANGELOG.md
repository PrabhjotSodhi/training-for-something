# Changelog

Versions follow Python's PEP 440. Each release starts with "Breaking changes". Each breaking change says what to use instead. Changes to `coach_v1` views and the ask TFS tool come first.

## Unreleased

### Breaking changes

- The ask TFS tool runs on the official MCP Python SDK. The tool schemas are generated, so their JSON layout differs. Tool names, arguments and results do not change. A failed call returns plain text that starts with `Error executing tool`, where it returned a JSON object with an `error` key.
- App keys moved to a `.env` file in the data folder. Run `tfs connect` again for each app.

### Added

- Design documents: the HLD, the code map and the plan items, proposal actions and metrics specs.
