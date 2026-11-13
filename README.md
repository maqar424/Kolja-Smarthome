# Kolja-Smarthome – lamp remote

Self-hosted PWA and automation service for the Zigbee lamps in Kolja's apartment. It talks to Zigbee2MQTT **only via MQTT** and replaces the wall-switch logic of the old `smarthome_hub` container.

- Spec: [`docs/requirements.md`](docs/requirements.md)
- Rules for working on this repo: [`CLAUDE.md`](CLAUDE.md)

> Status: milestone M6 done. Everything up to the sunrise wake-up, the phone app (floor plan, phase bar, colour, settings) and power history for the metering plugs. Next: lighting rendering (M7), deployment (M8).

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
uvicorn app.main:create_default_app --factory --port 8000                  # this machine only
uvicorn app.main:create_default_app --factory --host 0.0.0.0 --port 8000   # reachable from the phone
```

Open http://localhost:8000 (or `http://<vm-name>:8000` from the phone via Tailscale). Installing as an app ("Zum Startbildschirm hinzufügen" with offline shell) needs HTTPS: on the Pi via `tailscale serve` (M8).

UI checks without a phone: the app can be screenshotted at phone size with headless Firefox through geckodriver (WebDriver over HTTP, no extra Python packages).

- http://localhost:8000/health – MQTT, Zigbee2MQTT and clock status (HTTP 503 while degraded)
- http://localhost:8000/api/state – rooms and live lamp/plug state
- http://localhost:8000/api/debug/commands – last commands sent (or only logged in dry run)
- http://localhost:8000/api/schedule – current phase, today's phase bar, colour, settings
- http://localhost:8000/api/power/pveSwitch?range=24h – power history (`6h`, `24h`, `7d`, `30d`, `1y`): average and peak per bucket, energy
- http://localhost:8000/docs – interactive list of all endpoints

Try actions with curl (in dry run they are only logged):

```bash
curl -X POST localhost:8000/api/schedule/next                       # "Nächste Phase"
curl -X POST localhost:8000/api/rooms/Bad/dim -H 'content-type: application/json' -d '{"direction":"down"}'
curl -X POST localhost:8000/api/colour -H 'content-type: application/json' -d '{"master_ct":0.5}'
curl -X DELETE localhost:8000/api/colour                            # back to automatic
curl localhost:8000/api/sunrise                                     # sunrise settings, next run, warning
curl -X POST localhost:8000/api/sunrise/test                        # ~1 min test run (animation + alle_kalt)
```

State (settings, manual advances, room scenes) and the power history live in `DATA_DIR/smarthome.db` (SQLite). Power is sampled once a minute for all four metering plugs (pveSwitch read only) and kept for a year, about 60 MB.

With `DRY_RUN=true` every command is only logged (`DRY RUN  zigbee2mqtt/... {...}`). Lamp states are not retained by Zigbee2MQTT, so at startup the app asks each lamp once (`<lamp>/get`). In dry run that only happens with `DRY_RUN_ALLOW_READS=true`; otherwise states show up as lamps report changes.

## Deploy (Pi)

*Coming in M8.* Planned: `git pull && docker compose up -d --build`, HTTPS via `tailscale serve`.

## How it works (short)

- **One publish chokepoint** (`app/mqtt/publisher.py`): every MQTT command passes the hard-rule guards (never pveSwitch, no scene_recall, no colour to plugs, groups only "off", colour clamped to 153–370) and the dry-run switch.
- **Pure logic with an injected clock** (`app/domain/`): phases, colour and dimming are functions of settings, stored events and `now`. Tests jump through days with a fake clock; after a restart the app computes exactly where it should be.
- **Never steps backwards**: the phase timeline is one walk through all boundaries in time order; a boundary applies only if it moves the phase forward (kalt always starts a new day). A manual "next phase" is just another event in that walk.
- **Self-healing colour**: every 30 s the scheduler compares each lit lamp's *reported* colour with the target and corrects differences. That handles ramps, lamps powered on later, and lost commands the same way.
- **Sunrise records**: each day's run is stored (running / fallback / done / cancelled / missed). After a restart the record decides: resume the animation at the right point, apply the final state, or report a missed run. A run with problems leaves a warning (also in `/health`); a good run shows nothing.
- **Durations in UTC**: Python adds/subtracts aware local datetimes in wall-clock time, which is wrong on DST days. Durations are computed with UTC/timestamps; local time is only used for "07:00 today".
