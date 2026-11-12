"""Switching between laptop-only Studio and home-network (iPad) Studio cleanly.

    python -m camctl.launch --lan      # before starting in home-network mode
    python -m camctl.launch --local    # before starting laptop-only

If a Studio server already runs on the port in the OTHER mode, it is stopped cleanly (the camera session is closed
first) so the new start does not fail on a busy port. A server already in the wanted mode is left alone (exit code 3:
"already running as wanted"). Nothing else is touched (no firewall, no network settings).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def running_mode(port: int = 8770) -> str | None:
    """'lan', 'local' or None (nothing answering)."""
    try:
        cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/client_config", timeout=2).read())
    except Exception:  # noqa: BLE001
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)   # a PIN-gated (home-network) server
            return "lan"
        except Exception:  # noqa: BLE001
            return None
    return "lan" if (cfg.get("lan") or {}).get("on") else "local"


def stop(port: int = 8770, wait_s: float = 15.0) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST"), timeout=10).read()
    except Exception:  # noqa: BLE001
        pass
    end = time.time() + wait_s
    while time.time() < end:
        if running_mode(port) is None:
            return True
        time.sleep(0.3)
    return False


def prepare(want: str, port: int = 8770) -> int:
    """0 = free to start, 3 = already running in the wanted mode, 1 = could not stop the other one."""
    now = running_mode(port)
    if now is None:
        return 0
    if now == want:
        return 3
    return 0 if stop(port) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--lan", action="store_true")
    g.add_argument("--local", action="store_true")
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    r = prepare("lan" if a.lan else "local", a.port)
    print({0: "Free to start.", 3: "Studio is already running this way.", 1: "The running Studio did not stop."}[r])
    return r


if __name__ == "__main__":
    sys.exit(main())
