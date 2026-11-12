"""Battery guard for every camera test tool: check before each step, stop at the limit, and say so.

    from battery_guard import guard
    guard(15, "zoom run 3")      # exits with a plain sentence when the battery is at or below 15 %

Reads the battery from Studio (property 0xD218). If it cannot be read, it stops too (better safe than an empty camera).
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8770"
STOPPED: list[str] = []


def battery(base: str = BASE) -> int | None:
    try:
        props = json.loads(urllib.request.urlopen(base + "/api/props", timeout=20).read())
        v = next((p["value"] for p in props if p["code"] == "0xD218"), None)
        return int(v) if isinstance(v, int) and 0 <= v <= 100 else None
    except Exception:  # noqa: BLE001
        return None


def guard(limit: int, step: str, base: str = BASE) -> int:
    """Return the battery %, or stop the tool (exit code 3) with a plain sentence when it is at or below the limit."""
    b = battery(base)
    if b is None or b <= limit:
        msg = (f"Stopped before {step}: the battery is at {b}% (limit {limit}%)." if b is not None else
               f"Stopped before {step}: the battery level could not be read.")
        STOPPED.append(msg)
        print(msg, flush=True)
        sys.exit(3)
    print(f"[battery {b}% before {step}]", flush=True)
    return b
