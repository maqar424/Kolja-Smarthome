# Deploy to the Pi and switch over from `smarthome_hub`

Kolja runs these steps on the Pi. Each step can be checked before going on, and every step up to switch-over day can be undone with `docker compose down`, without touching anything else.

This app has its own container, `kolja-smarthome`. Nothing here changes `mosquitto_broker`, `zigbee2mqtt` or their data. Only step 5 touches `smarthome_hub`, and only by stopping it.

## 0. Before you start

- The app on the dev VM must not run with `DRY_RUN=false` at the same time as the Pi; two apps would both drive the lamps. On the VM, keep `DRY_RUN=true` in `.env`.
- On the Pi, check that port 8000 is free: `sudo ss -ltnp | grep ':8000 '` should print nothing. If it is taken (maybe by `smarthome_hub`), set `HTTP_PORT=8001` in `.env` in step 1 and use that port below.

## 1. Install (dry run)

```bash
cd ~
git clone https://github.com/maqar424/Kolja-Smarthome.git    # or git@github.com:... if the Pi has an SSH key
cd Kolja-Smarthome
cp .env.example .env
nano .env        # keep DRY_RUN=true and BUTTONS_ENABLED=false for now; MQTT_HOST does not matter here
mkdir -p data    # create it yourself, so it belongs to you (uid 1000) and not to root
docker compose up -d --build
```

The first build takes a few minutes on the Pi; later updates take seconds.

Check:

```bash
docker compose ps                      # STATUS shows "(healthy)" after about a minute
curl -s localhost:8000/health; echo    # "status":"ok", mqtt_connected, z2m_online, clock_synchronised all true
docker compose logs --tail 30          # "Config check: config matches Zigbee2MQTT"
```

If `id -u` on the Pi does not print `1000`, run `sudo chown 1000:1000 data` once (the container's user is uid 1000).

If the log shows no "Connected to MQTT" (e.g. a firewall on the Pi blocks containers from reaching the host): in `docker-compose.yml` replace `MQTT_HOST: host.docker.internal` with the Pi's LAN IP and run `docker compose up -d` again.

## 2. HTTPS via Tailscale

Once per tailnet: in the Tailscale admin console under **DNS**, enable **MagicDNS** and **HTTPS Certificates**.

On the Pi:

```bash
sudo tailscale serve --bg 8000
tailscale serve status        # shows https://rapi.<tailnet>.ts.net -> http://127.0.0.1:8000
```

On the phone (Tailscale on), open `https://rapi.<tailnet>.ts.net` and choose "Zum Startbildschirm hinzufügen". The first request may take a few seconds while the certificate is issued.

The LAN address `http://<pi-ip>:8000` keeps working as a fallback, but only the HTTPS address can be installed as an app.

To undo: `sudo tailscale serve reset`.

## 3. Go live with buttons still off

Now the app may send commands: phase changes, colour, the app's own buttons, sunrise. The wall switches still belong to the old hub.

```bash
nano .env     # DRY_RUN=false, BUTTONS_ENABLED=false
docker compose up -d     # recreates the container with the new settings (no rebuild needed)
```

Check in the app:
- Tap a lamp; it switches. "Alles aus" works.
- Settings: enter the phase times, location and sunrise again. The Pi starts with a fresh database; the dev VM's settings are not copied.
- Optional: the sunrise "Test" button runs the whole wake-up in about 1 minute.

Leave it like this for a day or two. Wall switches still work through the old hub; the app corrects the colour of lamps switched on that way.

## 4. Switch-over day: prepare

Have the phone with the app ready, and stand next to a wall switch.

## 5. Switch over

The order matters: the old hub must be stopped **before** buttons are enabled, so a press never triggers both.

```bash
docker stop smarthome_hub
docker update --restart=no smarthome_hub      # so a Pi reboot does not bring it back

cd ~/Kolja-Smarthome
nano .env      # BUTTONS_ENABLED=true
docker compose up -d
curl -s localhost:8000/health; echo     # "buttons_enabled":true
```

Test every switch:

| Switch | Test |
|---|---|
| Each of the four Lichtschalter | **I** (on): room on in the scene of the phase; again while on: scene re-applied at full level. **Brighter / darker** buttons: dim. **O** (off): room off |
| Flur switch, additionally | hold **I** for about 1.5 s: everything on; hold **O**: everything off |
| Kettle button (`kuecheButton`) | one press: kettle on, off again after 180 s; press while running: off immediately |

## 6. Rollback (if something is wrong)

```bash
cd ~/Kolja-Smarthome
nano .env      # BUTTONS_ENABLED=false
docker compose up -d
docker update --restart=unless-stopped smarthome_hub
docker start smarthome_hub
```

The app keeps working as a remote; the wall switches are back with the old hub. To stop the app completely: `docker compose down`. Its data stays in `./data`.

Once everything has run well for a while, the old container can be removed (`docker rm smarthome_hub`). That is Kolja's call, not part of this repo.

## Updates

```bash
cd ~/Kolja-Smarthome
git pull
docker compose up -d --build
```

Settings, room state and power history live in `./data` and survive updates and rebuilds. During the few seconds of the restart the wall switches don't react; a sunrise or kettle timer in progress is resumed after the restart.

## Backup

`./data/smarthome.db` is the only state. To copy it safely while the app runs:

```bash
docker compose exec lamp-remote python -c "import sqlite3; sqlite3.connect('/data/smarthome.db').backup(sqlite3.connect('/data/backup.db'))"
# then copy ./data/backup.db somewhere else
```

## Useful commands

```bash
docker compose logs -f            # live log (Ctrl+C to leave)
docker compose ps                 # running? healthy?
docker compose restart            # restart the app
curl -s localhost:8000/health     # status as JSON
```
