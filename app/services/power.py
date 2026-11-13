"""Power recording for the metering plugs (requirements §9).

Once a minute, on the full minute, the last power value each plug reported is
written to SQLite. The plugs only report when the load changes, so "last
reported value" is the best estimate of the current load. A sample is skipped
(a gap in the chart) when the value can't be trusted: Zigbee2MQTT offline, the
plug marked unavailable, or nothing reported since the app started.

Samples older than one year are deleted once a day.

History queries put the samples into buckets (average and peak per bucket),
so a chart gets a few hundred points whatever the range.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.clock import Clock
from app.config import AppConfig
from app.state import StateStore
from app.store import Store

log = logging.getLogger(__name__)

SAMPLE_S = 60
RETENTION_DAYS = 365


@dataclass(frozen=True)
class Range:
    seconds: int
    bucket_s: int


# ~300 points per range. A 3-minute kettle run stays visible in long ranges
# because each bucket also carries its peak.
RANGES = {
    "6h": Range(6 * 3600, 60),
    "24h": Range(24 * 3600, 5 * 60),
    "7d": Range(7 * 86400, 30 * 60),
    "30d": Range(30 * 86400, 2 * 3600),
    "1y": Range(365 * 86400, 86400),
}


class PowerRecorder:
    def __init__(self, config: AppConfig, state: StateStore, store: Store, clock: Clock) -> None:
        self.config = config
        self.state = state
        self.store = store
        self.clock = clock
        self.devices = [d.friendly_name for d in config.power_devices()]
        self.last_sample_at: datetime | None = None
        self._pruned_on = None

    async def run(self) -> None:
        while True:
            now = self.clock.now()
            ts = int(now.timestamp())
            nxt = datetime.fromtimestamp((ts // SAMPLE_S + 1) * SAMPLE_S, timezone.utc)
            await self.clock.sleep_until(nxt)
            try:
                self.sample()
                self.prune_daily()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - recording must never stop the app
                log.exception("Power sample failed")

    def sample(self) -> dict[str, float]:
        now = self.clock.now()
        samples: dict[str, float] = {}
        if self.state.bridge_online is True:
            for name in self.devices:
                device = self.state.get(name)
                if device.available is False or not isinstance(device.power, (int, float)):
                    continue
                samples[name] = float(device.power)
        ts = int(now.timestamp()) // SAMPLE_S * SAMPLE_S
        if samples:
            self.store.add_power_samples(ts, samples)
        self.last_sample_at = now
        return samples

    def prune_daily(self) -> None:
        today = self.clock.now().date()
        if self._pruned_on == today:
            return
        self._pruned_on = today
        cutoff = int(self.clock.now().timestamp()) - RETENTION_DAYS * 86400
        removed = self.store.prune_power(cutoff)
        if removed:
            log.info("Removed %d power samples older than %d days", removed, RETENTION_DAYS)

    def history(self, device: str, range_key: str) -> dict:
        rng = RANGES[range_key]
        now = self.clock.now()
        end = int(now.timestamp())
        start = end - rng.seconds
        shift = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
        rows = self.store.power_buckets(device, start, end + 1, rng.bucket_s, shift)
        total, n = self.store.power_sum(device, start, end + 1)
        return {
            "device": device,
            "range": range_key,
            "start": start,
            "end": end,
            "bucket_s": rng.bucket_s,
            "sample_s": SAMPLE_S,
            # [bucket start (unix s), average W, peak W]
            "points": [[b, round(avg, 1), round(peak, 1)] for b, avg, peak, _ in rows],
            "samples": n,
            # 1-minute samples: W * 1 min -> kWh. Only counts minutes with data.
            "energy_kwh": round(total * SAMPLE_S / 3600 / 1000, 3),
            "avg_w": round(total / n, 1) if n else None,
            "peak_w": round(max(r[2] for r in rows), 1) if rows else None,
        }
