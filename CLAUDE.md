# Kolja-Smarthome – lamp remote

Self-hosted PWA + automation service for the Zigbee lamps in Kolja's apartment. It replaces the old `smarthome_hub` container (wall switch logic) and is the phone remote for all lamps.

**The spec is `docs/requirements.md`. Read it fully before planning or changing behaviour.** If code and spec disagree, the spec wins; if the spec is unclear or contradicts itself, ask instead of guessing.

## Context files

| File | What it is |
|---|---|
| `docs/requirements.md` | Agreed requirements and decisions |
| `docs/devices.xlsx` | Kolja's master sheet: *Devices* (rooms, flags, positions), *Rooms*, *Scenes*, *SceneBrightness*, *Legend* |
| `docs/apartment-floorplan.svg` | Floor plan, 1 unit = 1 cm, room shapes have ids |
| `docs/z2m-snapshot.json` | Trimmed Zigbee2MQTT export: devices with capabilities (`exposes`), groups, scenes. Use as test fixture |

`devices.xlsx` is edited by Kolja in Excel and stays the human-facing master. The running app must **not** depend on Excel: propose a conversion step (e.g. a script that turns the sheet into versioned config files) in the plan.

## Environment

- **Dev:** this Linux VM (x86_64) on Proxmox. Claude Code runs here.
- **Prod:** Raspberry Pi 4, 64-bit (`aarch64`), Docker. Existing containers: `mosquitto_broker` (port 1883, anonymous, LAN only), `zigbee2mqtt` (`koenkk/zigbee2mqtt:2.12.1`, base topic `zigbee2mqtt`, UI on 8080), `smarthome_hub` (old, still active until switch-over).
- The broker is reachable from the VM at `<PI_IP>:1883` (put the real value in `.env`, never in code).
- The app is **its own container** and talks to Zigbee2MQTT **only via MQTT**.
- Deploy: the Pi pulls this repo from GitHub and builds the image locally (arm64): `git pull && docker compose up -d --build`.

## Stack (default – confirm or challenge in the plan)

- Python 3 + FastAPI, async MQTT client, local sun calculation (no internet needed), SQLite.
- Frontend: lightweight PWA that renders the SVG floor plan directly. No heavy framework unless the plan justifies it.
- One container, `restart: unless-stopped`, health check endpoint.
- Ask before adding large dependencies.

## Hard rules

1. **Never send any command to `pveSwitch`.** It powers the Proxmox host this VM runs on.
2. **Never touch Zigbee2MQTT's or Mosquitto's config, data or containers.** No Docker commands against existing containers on the Pi.
3. **Dry run by default in development:** a `DRY_RUN` setting that logs MQTT commands instead of publishing them. Publishing to the real broker only when Kolja explicitly enables it.
4. **`BUTTONS_ENABLED` defaults to off** until switch-over day, so the old `smarthome_hub` and this app never both react to a button press.
5. **Scenes are owned by the app** (from *SceneBrightness*). Never use Zigbee `scene_recall` / `scene_store`.
6. **Colour:** one global value, common range 153–370 mired for every lamp, never per-lamp colour. Never send colour or brightness to on/off-only plugs.
7. **Never commit** `.env`, Zigbee2MQTT backups (`*.zip`, contain the network key) or raw web-UI state exports. Provide `.env.example`.

## Testing

- The time-based logic must be testable with an injected clock: colour ramps, day phases incl. sun mode and "never step backwards", dimming factor limits, sunrise timeline, fallback and resume after restart.
- Use `docs/z2m-snapshot.json` as fixture. A small fake Zigbee2MQTT (answers `/set` with state updates) for integration tests on a local broker is welcome.
- The sunrise wake-up must be dependable: cover it with tests before anything cosmetic.

## Working style

- Kolja is learning git: small, focused commits with clear messages. **Ask before pushing.**
- UI text in **German**; code, comments, commit messages and docs in English.
- Keep a `README.md` with setup, run, test and deploy commands up to date.
- Explain notable design decisions briefly; Kolja wants to understand the system, not just receive it.

## Suggested milestone order (refine in the plan)

1. Core: config import, MQTT connection, live lamp state, dry run, health check.
2. Colour model, day phases (manual + sun mode), scenes, dimming.
3. Wall switches (behind `BUTTONS_ENABLED`) incl. Flur hold for all on/off.
4. Sunrise wake-up incl. test button.
5. PWA: floor plan tab with lamps, phase bar + next-phase button, settings tab.
6. Plugs tab with power history.
7. Live lighting rendering.
8. Container + deployment to the Pi, switch-over plan from `smarthome_hub`.
