# Kolja-Smarthome – lamp remote

Self-hosted PWA and automation service for the Zigbee lamps in Kolja's apartment. It talks to Zigbee2MQTT **only via MQTT** and replaces the wall-switch logic of the old `smarthome_hub` container.

- Spec: [`docs/requirements.md`](docs/requirements.md)
- Rules for working on this repo: [`CLAUDE.md`](CLAUDE.md)

> Status: early development (milestone M1, core). The app connects to MQTT, tracks live lamp state and serves `/health` and `/api/state`. No automation or UI yet.

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
pytest                                              # unit tests
docker compose -f docker-compose.test.yml up -d     # local throwaway broker on 127.0.0.1:18830
pytest                                              # now also runs the MQTT integration tests
docker compose -f docker-compose.test.yml down
```

The integration tests use a fake Zigbee2MQTT (`tests/fake_z2m.py`) fed from `docs/z2m-snapshot.json`. Without the test broker they are skipped. They never talk to the Pi.

## Config from Excel

`docs/devices.xlsx` is the master. After editing it:

```bash
python tools/import_sheet.py           # validates, then writes config/*.json
python tools/import_sheet.py --check   # validate only
git add docs/devices.xlsx config/ && git commit
```

Errors (sheet inconsistent with itself) stop the import; warnings (sheet vs. Zigbee2MQTT snapshot, lamp outside its room) are printed but the files are still written. A test fails if `config/` is stale, so a forgotten import is caught by `pytest`.

## Run (dev VM)

```bash
source .venv/bin/activate
uvicorn app.main:app --port 8000
```

- http://localhost:8000/health – MQTT, Zigbee2MQTT and clock status (HTTP 503 while degraded)
- http://localhost:8000/api/state – rooms and live lamp/plug state
- http://localhost:8000/api/debug/commands – last commands sent (or only logged in dry run)

With `DRY_RUN=true` every command is only logged (`DRY RUN  zigbee2mqtt/... {...}`). Lamp states are not retained by Zigbee2MQTT, so at startup the app asks each lamp once (`<lamp>/get`). In dry run that only happens with `DRY_RUN_ALLOW_READS=true`; otherwise states show up as lamps report changes.

## Deploy (Pi)

*Coming in M8.* Planned: `git pull && docker compose up -d --build`, HTTPS via `tailscale serve`.
