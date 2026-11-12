"""HTTP API for the PWA (and curl during development).

All actions go through the services; nothing here talks to MQTT directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.domain.colour import to_mired

if TYPE_CHECKING:
    from app.main import Services


class ColourIn(BaseModel):
    master_ct: float = Field(ge=0, le=1)


class DimIn(BaseModel):
    direction: Literal["up", "down"]


class SceneIn(BaseModel):
    scene: Literal["kalt", "warm", "twilight", "night"]


class LampIn(BaseModel):
    on: bool | None = None
    brightness: int | None = Field(default=None, ge=1, le=254)


def build_router(services: "Services") -> APIRouter:
    router = APIRouter(prefix="/api")
    cfg = services.config
    lights = services.lights
    schedule = services.schedule

    def room_or_404(room: str) -> str:
        if room not in cfg.rooms:
            raise HTTPException(404, f"Unbekannter Raum: {room}")
        return room

    # ------------------------------------------------------------------ payloads (shared by endpoints and /events)

    def state_payload() -> dict:
        return {
            "rooms": [
                {
                    "room": r.room,
                    "display_name": r.display_name,
                    "svg_id": r.svg_id,
                    "lamps": [d.friendly_name for d in cfg.lamps(r.room)],
                    "on": bool(services.state.lamps_on(r.room)),
                    **services.store.room_light(r.room).to_dict(),
                }
                for r in cfg.rooms.values()
            ],
            "devices": {
                d.friendly_name: {
                    "display_name": d.display_name,
                    "room": d.room,
                    "dimmable": d.dimmable,
                    "has_colour": d.has_colour,
                    "has_power": d.has_power,
                    "locked": d.friendly_name in services.lights.locked,
                    **services.state.get(d.friendly_name).snapshot(),
                }
                for d in cfg.devices.values()
                if d.is_lamp or d.has_power
            },
        }

    def system_payload() -> dict:
        mqtt = services.mqtt
        return {
            "dry_run": services.settings.dry_run,
            "buttons_enabled": services.settings.buttons_enabled,
            "mqtt_connected": bool(mqtt and mqtt.connected),
            "z2m_online": services.state.bridge_online is True,
        }

    def snapshot() -> dict:
        return {
            "state": state_payload(),
            "schedule": schedule_payload(),
            "sunrise": services.sunrise.status(),
            "system": system_payload(),
        }

    router.snapshot = snapshot  # used by the /events stream in main.py

    @router.get("/state")
    async def api_state():
        return state_payload()

    @router.get("/snapshot")
    async def api_snapshot():
        return snapshot()

    @router.get("/config")
    async def api_config():
        """Static description for the UI: rooms, lamps with shape/position, scenes."""
        return {
            "rooms": [
                {"room": r.room, "display_name": r.display_name, "svg_id": r.svg_id}
                for r in cfg.rooms.values()
            ],
            "lamps": [
                {
                    "name": d.friendly_name,
                    "display_name": d.display_name,
                    "room": d.room,
                    "dimmable": d.dimmable,
                    "has_colour": d.has_colour,
                    "shape": d.shape,
                    "geometry": d.geometry,
                    "mount": d.mount,
                    "height_cm": d.height_cm,
                }
                for d in cfg.lamps()
            ],
            "plugs": [
                {"name": d.friendly_name, "display_name": d.display_name, "room": d.room, "is_lamp": d.is_lamp}
                for d in cfg.power_devices()
            ],
            "scenes": [
                {"key": s.key, "display_name": s.display_name}
                for s in sorted(cfg.scenes.values(), key=lambda s: s.sort_order)
                if s.show_in_app and s.key != "off"
            ],
        }

    # ------------------------------------------------------------------ schedule / colour

    def schedule_payload() -> dict:
        now = services.clock.now()
        timeline = schedule.timeline(now)
        colour = schedule.colour(now, timeline)
        nxt = timeline.next_boundary(now)
        return {
            "now": now.isoformat(timespec="seconds"),
            "phase": timeline.phase,
            "can_advance": timeline.can_advance(),
            "next_phase": timeline.next_phase(),
            "next_boundary": {"phase": nxt.phase, "at": nxt.at.isoformat(timespec="seconds")} if nxt else None,
            "segments": [
                {"phase": s.phase, "start": s.start.isoformat(timespec="seconds"),
                 "end": s.end.isoformat(timespec="seconds")}
                for s in timeline.segments(now.date())
            ],
            "colour": {
                "master_ct": round(colour.master_ct, 4),
                "mired": to_mired(colour.master_ct),
                "ramping": colour.ramping,
                "override": colour.override,
            },
            "settings": schedule.settings.to_dict(),
        }

    @router.get("/schedule")
    async def get_schedule():
        return schedule_payload()

    @router.post("/schedule/next")
    async def next_phase():
        phase = schedule.advance()
        if phase is None:
            raise HTTPException(409, "In der Nacht gibt es keine nächste Phase.")
        return {"phase": phase}

    @router.put("/schedule/settings")
    async def put_settings(data: dict):
        errors = schedule.update_settings(data)
        if errors:
            raise HTTPException(400, {"errors": errors})
        return schedule.settings.to_dict()

    @router.post("/colour")
    async def set_colour(body: ColourIn):
        schedule.set_override(body.master_ct)
        return {"master_ct": body.master_ct, "mired": to_mired(body.master_ct)}

    @router.delete("/colour")
    async def colour_automatic():
        schedule.clear_override()
        return {"override": False}

    # ------------------------------------------------------------------ rooms / lamps

    @router.post("/rooms/{room}/on")
    async def room_on(room: str):
        await lights.room_on(room_or_404(room))
        return {"ok": True}

    @router.post("/rooms/{room}/off")
    async def room_off(room: str):
        await lights.room_off(room_or_404(room))
        return {"ok": True}

    @router.post("/rooms/{room}/dim")
    async def room_dim(room: str, body: DimIn):
        changed = await lights.dim(room_or_404(room), body.direction)
        return {"changed": changed}

    @router.post("/rooms/{room}/scene")
    async def room_scene(room: str, body: SceneIn):
        await lights.apply_scene(room_or_404(room), body.scene, transition=0.4, explicit=True)
        return {"ok": True}

    @router.post("/lamps/{name}")
    async def lamp(name: str, body: LampIn):
        device = cfg.devices.get(name)
        if device is None or not device.is_lamp:
            raise HTTPException(404, f"Unbekannte Lampe: {name}")
        if name in lights.locked:
            raise HTTPException(409, "Sonnenaufgang läuft")
        ok = await lights.set_lamp(name, on=body.on, brightness=body.brightness)
        return {"ok": ok}

    @router.post("/all-off")
    async def all_off():
        await lights.all_off()
        return {"ok": True}

    @router.post("/all-on")
    async def all_on():
        await lights.all_on()
        return {"ok": True}

    # ------------------------------------------------------------------ sunrise

    sunrise = services.sunrise

    @router.get("/sunrise")
    async def sunrise_status():
        return sunrise.status()

    @router.put("/sunrise/settings")
    async def sunrise_settings(data: dict):
        errors = sunrise.update_settings(data)
        if errors:
            raise HTTPException(400, {"errors": errors})
        return sunrise.status()

    @router.post("/sunrise/cancel")
    async def sunrise_cancel():
        if not sunrise.cancel():
            raise HTTPException(409, "Es läuft gerade kein Sonnenaufgang.")
        return {"ok": True}

    @router.post("/sunrise/test")
    async def sunrise_test():
        if not sunrise.start_test():
            raise HTTPException(409, "Es läuft bereits ein Sonnenaufgang.")
        return {"ok": True}

    @router.post("/sunrise/dismiss")
    async def sunrise_dismiss():
        sunrise.dismiss_warning()
        return {"ok": True}

    return router
