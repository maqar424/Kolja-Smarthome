"""Is the system clock NTP-synchronised?

The Pi has no hardware clock, and the sunrise depends on correct time. We ask
the kernel directly with a read-only adjtimex() call: it returns TIME_ERROR (5)
while the clock is unsynchronised. This works inside a container without
mounting anything or needing extra capabilities (modes = 0 changes nothing).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger(__name__)

TIME_ERROR = 5


def clock_synchronised() -> bool | None:
    """True/False, or None if it cannot be determined (non-Linux)."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(1024)  # >= sizeof(struct timex); zeroed -> modes = 0 (read only)
        result = libc.adjtimex(buf)
    except (OSError, AttributeError):
        return None
    if result < 0:
        log.debug("adjtimex failed, errno %s", ctypes.get_errno())
        return None
    return result != TIME_ERROR
