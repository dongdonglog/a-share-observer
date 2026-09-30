# Instructions for AI coding agents

This repository contains A股观察站 (`a-share-observer`), a local A-share market
data and review dashboard. Read `docs/AI_SETUP.md` when installing it on a new
computer, and read the relevant section of `README.md` before changing behavior.

## Setup and operation

- Python 3.10 or newer is required. The application uses the Python standard
  library and has no third-party Python dependencies.
- On macOS, `./setup.sh --collect --autostart` creates a virtual environment,
  performs the first public market-data collection, and installs a user
  LaunchAgent. The collection and autostart steps are explicit because they
  contact public data sources and register a persistent service.
- `./setup.sh` prepares the environment and local database without network
  collection. Start the dashboard with `./.venv/bin/python run.py dash`.
- On macOS, `./tools/dash.sh status|logs|stop|uninstall` manages the resident
  dashboard. It listens on `127.0.0.1:8787` by default.
- LLM support is optional. Configure it with `python run.py llm-setup` or the
  documented `CHAOGU_LLM_*` environment variables. Never put credentials in
  source files or commit local configuration.

## Repository and data boundaries

- `data/`, `reports/`, `.dash.log`, `.dash.pid`, `.workflows/`, `_incoming/`,
  and `.topic-canon.txt` are local state or generated material. Keep them out
  of commits unless the user specifically asks to publish a reviewed sample.
- Do not delete or replace a user's local database as part of installation or
  maintenance. Back it up before any requested migration.
- Do not trigger market-data collection unless the user asked to collect data
  or explicitly selected an install command containing `--collect`.
- The dashboard is research software. Preserve its caveats: it does not issue
  personalized investment advice or guarantee returns.

## Useful commands

```bash
./.venv/bin/python run.py status
./.venv/bin/python run.py daily
./.venv/bin/python run.py report
./.venv/bin/python run.py dash
```
