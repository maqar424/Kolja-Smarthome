# Kolja-Smarthome – lamp remote

Self-hosted PWA and automation service for the Zigbee lamps in Kolja's apartment. It talks to Zigbee2MQTT **only via MQTT** and replaces the wall-switch logic of the old `smarthome_hub` container.

- Spec: [`docs/requirements.md`](docs/requirements.md)
- Rules for working on this repo: [`CLAUDE.md`](CLAUDE.md)

> Status: early development (milestone M0, project skeleton). Nothing runs yet.

## Setup (dev VM)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then set MQTT_HOST to the Pi's IP
```

Keep `DRY_RUN=true` and `BUTTONS_ENABLED=false` in `.env` during development.

## Test

```bash
source .venv/bin/activate
pytest
```

## Config from Excel

`docs/devices.xlsx` is the master. After editing it:

```bash
python tools/import_sheet.py   # writes config/*.json (coming in M1)
git add docs/devices.xlsx config/ && git commit
```

## Run

*Coming in M1.*

## Deploy (Pi)

*Coming in M8.* Planned: `git pull && docker compose up -d --build`, HTTPS via `tailscale serve`.
