# Requirements – Lamp remote (Kolja-Smarthome)

Status: **draft, decisions agreed 2026-11-06.** Items marked *(default)* were proposed and accepted without discussion; items under "Open" are still missing.

## 1. Goal

A self-hosted web app (PWA) on the Raspberry Pi that

1. replaces the old `smarthome_hub` container completely (button logic), and
2. is the phone remote for all Zigbee lamps in the apartment.

It is opened in the phone browser and can be installed to the home screen.

## 2. Environment (as-is)

| Component | Details |
|---|---|
| Raspberry Pi 4 | 64-bit OS (`aarch64`), clock NTP-synchronised. Docker host. Containers: `mosquitto_broker`, `zigbee2mqtt`, `smarthome_hub` (old, to be removed) |
| MQTT broker | `eclipse-mosquitto`, port 1883 published on the Pi, anonymous (LAN only) |
| Zigbee2MQTT | `koenkk/zigbee2mqtt:2.12.1`, base topic `zigbee2mqtt`, frontend on port 8080, talks to broker as `mqtt://mosquitto:1883` |
| Coordinator | Network coordinator (Z-Stack) at `192.168.0.198:6638` |
| Dev machine | Linux VM on Proxmox (x64). Claude Code runs here. The Pi is ARM64 |

The new app is **its own container**. It only speaks MQTT; it never touches Zigbee2MQTT's config, data folder or container.

## 3. Data sources

| File | Content | Owner |
|---|---|---|
| `docs/devices.xlsx` | Master sheet, edited by Kolja | Kolja |
| `docs/devices.csv` | Export of sheet *Devices* | generated |
| `docs/rooms.csv` | Export of sheet *Rooms* | generated |
| `docs/scenes.csv` | Export of sheet *Scenes* | generated |
| `docs/scene_brightness.csv` | Export of sheet *SceneBrightness* | generated |
| `docs/apartment-floorplan.svg` | Floor plan, 1 unit = 1 cm, room shapes have ids | Kolja |
| `docs/z2m-snapshot.json` | Trimmed Z2M export: devices with capabilities (`exposes`), groups, scenes. Use as test fixture | generated |

Live truth for capabilities and state is Zigbee2MQTT over MQTT (`zigbee2mqtt/bridge/devices`, `zigbee2mqtt/bridge/groups`, `zigbee2mqtt/<name>`). The CSVs add what Z2M does not know (room, display name, flags, positions, scene brightness).

### Lamp positions (sheet *Devices*)

Coordinates in cm on `apartment-floorplan.svg` (1 SVG unit = 1 cm, origin top-left, plan 685 × 1285).

| Column | Meaning |
|---|---|
| `shape` | `point` = `"x,y"` · `line` = `"x1,y1 x2,y2 …"` (polyline) · `rect` = `"x,y,w,h"` (top-left + size as drawn) |
| `geometry_cm` | Coordinates in the format above. Several parts separated by `;` (e.g. `wzBodenstrips` = two strips, `kuecheTwintowers` = two towers) |
| `mount` | `floor`, `middle` (between floor and ceiling), `ceiling`, or `vertical` (standing light column) |
| `height_cm` | Only for `vertical`: span, e.g. `0-180` |

### Rooms and groups

| Room (Z2M group) | Group id | Floor plan shape id | Note |
|---|---|---|---|
| Wohnzimmer | 4 | `schlafzimmer` | **Shown as "Schlafzimmer" in the app.** Z2M group and device names (`wz…`) stay as they are |
| Küche | 3 | `kueche` | |
| Bad | 2 | `bad` | |
| Flur | 1 | `flur` | |
| `Alle_Lampen` | 5 | – | All lamps with `in_all_off = yes` |

## 4. Colour model

- One global value **`master_ct` ∈ [0, 1]**, 0 = coldest, 1 = warmest. All lamps always have the same colour; colour is never set per lamp.
- **Common range for every lamp: 153–370 mired** (the range every model supports). Mapping: `mired = 153 + master_ct × (370 − 153)`, linear in mired. Hue lamps are deliberately not driven warmer than 370 so all lamps match.
- On/off-only lamps (smart plugs) never receive colour or brightness.
- Every command that turns a lamp on carries brightness **and** colour in the same message (no flash of an old colour).
- Lamps that are already on follow `master_ct` while it changes: update about once a minute in small steps with a smooth transition *(default)*. Because the range is common, one command to group `Alle_Lampen` is acceptable for colour updates.
- If any lamp turns on by a route outside the app (Z2M UI, power restored), the app sets it to `master_ct` immediately; its brightness is left alone *(default)*.
- Scene colour values are always overridden by `master_ct`.

### Colour schedule

- `master_ct` ramps **0 → 1 starting at the beginning of phase "warm", over 60 min**, and **1 → 0 starting at the beginning of phase "kalt", over 30 min**. Both durations editable in settings.
- The master slider in the app overrides the schedule **until the next scheduled ramp starts**, then the schedule resumes. A "back to automatic" control ends the override *(default)*.

## 5. Day phases and scenes

Four phases, each with one scene. Scene names are the same in every group (Z2M ids: 0 off, 1 kalt, 2 warm, 3 night, 4 twilight).

| Phase / scene | Default start (manual mode) | Sun mode |
|---|---|---|
| kalt | 07:00 | stays fixed |
| warm | 18:00 | sunset, clamped to 18:00–21:00 |
| twilight | 21:00 | sunset + 1 h, clamped to 21:00–22:00 |
| night | 22:30 | stays fixed |

- All start times are editable in the app.
- Toggle **"Nach Sonnenstand"**: when on, phases with a sun rule use it (sun event + offset, limited by earliest/latest time); when off, the fixed times apply.
- Sun position is computed locally (no internet) for Köln (≈ 50.94 N, 6.96 E), location editable. Time zone Europe/Berlin incl. DST.
- **Scenes are owned by the app**, defined in `scene_brightness.csv` (per lamp: brightness 1–100 % or off; plugs on/off). Do **not** use Zigbee `scene_recall`: Z2M does not expose stored scene brightness over MQTT, and recall would flash the scene's own colour.
- **At a phase change, rooms that are on fade automatically to the new phase scene**; the room's dim factor resets to 1. Rooms that are off stay off.

### Manual "next phase"

- A button in the app moves the day phase one step forward **now**: kalt → warm → twilight → night. It behaves exactly like an automatic phase change: rooms that are on fade to the new scene, **no lamp changes its on/off state**, wall switches use the new phase from then on.
- In phase **night** the button does nothing (disabled).
- Colour changes to the new phase's colour within a few seconds (no 60-min ramp). A manual colour override from the master slider ends.
- **The schedule never steps backwards:** after a manual advance, the schedule takes over again at its next boundary that leads to a *later* phase, or at the start of `kalt` the next morning. Example: advanced to night at 20:00 → the scheduled twilight at 21:00 is skipped, the day restarts with kalt at 07:00.

## 6. Brightness and dimming

- Each room has an **active scene** and a **dim factor `f`** (starts at 1).
- Lamp brightness = scene brightness × `f` (Z2M brightness 0–254, rounded).
- Each dim press multiplies `f` by **1.15 (up)** or **0.85 (down)**.
- **Up** may exceed the scene; it stops when the brightest lamp of the room reaches 100 %.
- **Down** stops at the last step before any lamp would round to 0. Dimming never turns a lamp off.
- Ratios between lamps are preserved (lamp at 100 % changes twice as much as a lamp at 50 %).
- Only lamps that are on in the room are affected; on/off-only lamps ignore dimming.
- Brightness changes from the app on a single lamp set that lamp directly *(default)*.

## 7. Wall switches (Hue dimmer switch gen 2, `*Lichtschalter`, one per room)

Only these four switches are in scope. Only the press matters (duration irrelevant, no cycling), except the Flur hold rule.

| Button | Action |
|---|---|
| Top (`on_press`) | Room off → turn on with the **current phase scene**, `f = 1`. Room already on → restore the scene's original brightness (`f = 1`). |
| Up (`up_press`) | Dim factor up (see 6) |
| Down (`down_press`) | Dim factor down (see 6) |
| Bottom (`off_press`) | Turn off all lamps of the room with `in_room_off = yes` |

**Flur exception (`flurLichtschalter`):** the press acts immediately as above. If the top button is still held **1.5 s after the press**, trigger **all on** (= top-press logic in every room). Same for the bottom button → **all off**. Time the 1.5 s from the press event; never infer it from a missing release event.

**All off:** switch off group `Alle_Lampen`, check states after ~2 s, and resend individually to any lamp still on (group commands are unacknowledged).

**Coexistence with the old hub:** button handling must be switchable (e.g. `BUTTONS_ENABLED`), **default off** until switch-over day, so the old `smarthome_hub` and the new app never both react to a press.

## 7a. Sunrise wake-up

An animated sunrise in the Schlafzimmer that ends at a set wake-up time, followed by lighting up the whole apartment. **This feature must be dependable**: it runs entirely server-side, independent of the phone and the web UI.

### Settings (Settings tab)

| Setting | Meaning |
|---|---|
| Wake-up time | The time the animation **ends** |
| Duration | How long the animation runs; it starts this long before the wake-up time (default 30 min) |
| Toggle **"Sonnenaufgang"** | On = the sunrise runs **every day** at the wake-up time until the toggle is turned off. Off = it never runs. The toggle only changes when Kolja changes it. |

### Normal animation

Lamps: `wzBodenstrips`, `wzSchreibtischlampe`, `wzDeckenlampe`. Animation time `t` runs from 0 (start) to 1 (wake-up time).

| Lamp | On at | Brightness |
|---|---|---|
| `wzBodenstrips` | t = 0 | 0 → 100 % over t = 0 … 1 |
| `wzSchreibtischlampe` | t = 1/3 | 0 → 100 % over t = 1/3 … 1 |
| `wzDeckenlampe` | t = 2/3 | 0 → 100 % over t = 2/3 … 1 |

- "0 %" means the lowest brightness at which the lamp is on (Z2M brightness 1), never off.
- Brightness follows a **perceptually even curve** (not linear in Z2M values), so the dark start does not look like a jump.
- **Colour:** one shared ramp for all three lamps from warmest to coldest of the common range (370 → 153 mired) over t = 0 … 1. During the animation this overrides `master_ct`.
- Updates in small steps every few seconds with Zigbee transitions, so no stepping is visible.
- **At t = 1:** apply scene `kalt` in every room (= `alle_kalt`), fading in over a few seconds. Brightness comes from the scene grid, so the three animated lamps may change level or turn off at that moment if the kalt scene says so.

### Start of the day

If the wake-up time is earlier than the start of phase `kalt`, the day starts at the wake-up time: on that day, phase `kalt` begins at the wake-up time and `master_ct` is set to 0 (coldest) immediately, with no extra ramp. The schedule then continues normally (warm, twilight, night). If the wake-up time is after `kalt` has started, the schedule is not changed.

### Fallback: Schlafzimmer already lit

If **any** Schlafzimmer lamp is on when the animation would start, there is no animation. Instead, at the wake-up time:

1. `wzDeckenlampe` is set to 100 % brightness and coldest colour. The command is **always sent**, even if the lamp is already on in that state.
2. Scene `kalt` is applied in every room (= `alle_kalt`), exactly as at the end of the normal animation. `wzDeckenlampe` stays at 100 % even if the kalt scene defines a lower value.

### No interference

- **No wall switch, dial or button can change or stop a running sunrise.** During the animation, presses on `wzLichtschalter` are ignored; "all on"/"all off" from the Flur switch leave the three animated lamps alone. Other rooms work normally.
- The app shows the three lamps as "Sonnenaufgang läuft" and offers exactly one action: **cancel**. Cancelling stops today's animation and the final `alle_kalt`; lamps stay as they are. The toggle stays on, so it runs again the next day.

### Reliability

- Armed state, wake-up time and duration are persisted. If the app restarts during the animation, it resumes at the correct point. If it restarts after the wake-up time but within the animation's duration, it still applies the final state.
- Critical commands (start, final `alle_kalt`, fallback ceiling lamp) are **verified** against the state Z2M reports and resent individually if a lamp did not follow.
- Every run is logged. The app shows **nothing when a run succeeded**; only if something went wrong (e.g. a lamp did not respond, Z2M was offline, clock not synchronised) it shows a clear warning until it is dismissed.
- **"Test" button** in settings: plays the full sequence (animation and final `alle_kalt`) compressed to about 1 minute.
- Depends on correct time on the Pi (no hardware clock): the health check reports whether the system clock is NTP-synchronised.

## 8. App UI (PWA)

Three tabs:

| Left: Plugs | Middle: Floor plan (start screen) | Right: Settings |
|---|---|---|
| Non-lamp plugs (`pveSwitch`, `kuecheWasserkocher`): live power, read-only, no switching *(default)*. Tap → pop-up chart of power history | `apartment-floorplan.svg` with lamp markers at `pos_x_cm`/`pos_y_cm`, showing on/off and brightness. Tap lamp → on/off, brightness. Tap room → room on/off, dim, scene. Master colour slider and "Alles aus" | Phase times, sun toggle, colour ramp durations, location, back-to-automatic, sunrise wake-up (toggle "Sonnenaufgang", time, duration, "Test") |

- **UI language: German.** Code, comments and docs in English.
- Optimised for phone portrait; works on desktop too.
- Live state updates (lamps switched by wall switches appear immediately).

### Phase bar and "next phase" button (floor plan tab, next to each other)

- A thin horizontal bar for **today**: 00:00 at the left edge, 23:59 at the right. Segment lengths are proportional to the real phase times.
- Colours: **night = black**, **kalt = blue**, **warm = orange**, **twilight = a less bright orange**. Night appears twice (before kalt and after night begins).
- A marker shows the current time.
- The bar shows **today's effective times**: sun-mode times computed for today, edited times immediately, an early day start from the sunrise wake-up, and a manual advance (the boundary moves to the moment of the press).
- The "next phase" button sits right next to the bar and is disabled during night.

### Floor plan tab in detail

- Each lamp is drawn at its real position and shape (`shape`, `geometry_cm`): dot, line/strip or panel.
- **Tap a lamp:** toggle on/off. **Brightness:** a slider for the selected lamp (e.g. on long-press or in a small bottom sheet). On/off-only lamps (plugs) have no slider.
- **Colour is global only:** one master colour slider for the whole apartment; no per-lamp colour anywhere in the UI.

### Live lighting rendering

A rough visual impression of the light in the apartment, updated live from lamp state. It must look good but stay cheap.

- The unlit plan is shown dimmed; every lamp that is on adds a soft glow in the current master colour (mired → Kelvin → RGB approximation), intensity proportional to its brightness. Glows add up (additive / screen blending).
- Glow shape follows the geometry: radial for points and panels, a soft band along lines and strips.
- Spread depends on `mount`: `ceiling` = wide and soft (lights most of the room), `middle` = medium, `floor` = narrow and strong near the source, `vertical` = medium.
- Each glow is **clipped to its room's shape** (walls block light). Light leaking through door openings is optional, later.
- Approximate, not physically correct. No continuous animation: redraw only when state changes, with a short fade. SVG gradients/filters or Canvas 2D; no WebGL needed. Must stay smooth on a mid-range phone.

## 9. Power recording

- Record power for **all** smart plugs with metering (`TS011F`: pveSwitch, kuecheWasserkocher, wzTowerlampe, wzLavalampe) *(default)*.
- 1-minute samples, kept for **1 year** *(default)*. Stored locally (SQLite).

## 10. Reliability and safety

- After the switch-over the app is critical: if it is down, no wall switch works. Container `restart: unless-stopped`, health check endpoint, button logic independent of the web UI.
- State that must survive a restart: phase settings, override, active scene and dim factor per room, power history.
- On startup: read current lamp states from Z2M, then check config consistency (e.g. lamps with `in_all_off = yes` vs. members of `Alle_Lampen`, devices in CSV missing in Z2M) and log warnings.
- **Never** send commands to `pveSwitch` (powers the Proxmox host that runs the dev VM).
- Development runs against the real broker: provide a **dry-run mode** that logs MQTT commands instead of sending them, and tests that use `docs/z2m-snapshot.json` as fixture.
- Secrets/host config via `.env` (not committed); provide `.env.example`.

## 11. Out of scope (for now)

Hue tap dial switches, Hue smart buttons (incl. kettle button and kettle timer), fingerbot, switching `pveSwitch`, colour per lamp, drag-and-drop lamp positioning (later), migrating code from `smarthome_hub` (start fresh).

## 12. Stack and deployment *(default, Claude Code proposes details in plan mode)*

- Backend: Python + FastAPI, async MQTT client, scheduler, local sun calculation.
- Storage: SQLite.
- Frontend: lightweight PWA rendering the SVG floor plan.
- One container, built on the Pi (ARM64) from the git repo; deploy = `git pull` + `docker compose up -d --build`.

## 13. Open

- [x] Lamp positions and mounting height (sheet *Devices*)
- [x] Scene brightness per lamp (sheet *SceneBrightness*, from Zigbee scenes, reviewed)
- [x] Room display name: Schlafzimmer
- [x] UI language: German
- [ ] Pi IP address, fixed via DHCP reservation?
- [x] Verified: VM reaches the broker on port 1883
- [x] `CLAUDE.md`
