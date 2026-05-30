# agent instructions

## cursor cloud specific instructions

- standard setup, lint, and test commands are documented in `CONTRIBUTING.md`; after the cursor cloud update script has run, prefer the local `.venv/bin/python`, `.venv/bin/pytest`, and `.venv/bin/ruff` executables.
- for a quick local runtime check that avoids writing repo data or depending on external network targets, run the collector with `SMOKEMON_DB=/tmp/smokemon-cloud/smokemon.db` and `SMOKEMON_TARGETS=127.0.0.1`.
- the hub is optional for local node development; when starting it in cloud, bind to `127.0.0.1` and use an explicit non-default port if 8765 is already occupied.
- this checkout does not contain a frontend or npm manifest. if a task mentions npm, frontend, or `three`, first verify that the correct repository or subdirectory is present before running npm commands.
