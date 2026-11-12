"""After any test run on the real camera: put the exposure back to what Studio's LAST SETUP chose for the light now.

    python tools/leave_as_set_up.py [--port 8770] [--check]

"Leave the camera as found" means the values the Operator's setup chose (its 'chosen' record), never numbers from an
earlier session. 12 Nov 2026: a test restored last night's dark-room ISO 6400 over today's setup (ISO 3200), and the
picture went white. --check only reports the differences. Never shoots, never records, never touches picture
profile, file format or focus.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

KEYS = ("exposure_mode", "aperture", "shutter_speed", "iso", "white_balance", "color_temp")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    get = lambda p: json.loads(urllib.request.urlopen(base + p, timeout=15).read())
    chosen = (get("/api/autofix").get("chosen") or {})
    if not chosen:                                   # after a restart: the server reads the last setup's saved values
        chosen = get("/api/autofix").get("last_setup") or {}   # (not this process: an app sandbox may redirect AppData)
    if not chosen:
        print("No setup has chosen values yet: run a setup (Analyse again) instead of guessing values.")
        return 1
    now = {s["name"]: s["value"] for s in get("/api/settings")["settings"]}
    diff = [(k, now.get(k), chosen[k]) for k in KEYS if k in chosen and now.get(k) != chosen[k]]
    if not diff:
        print("The camera is exactly as the last setup chose.")
        return 0
    for k, cur, want in diff:
        print(f"{k}: camera {cur}, setup chose {want}")
    if a.check:
        return 2
    for k, _cur, want in diff:
        req = urllib.request.Request(base + "/api/set", json.dumps({"name": k, "value": want}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print(f"{k}: set back to {want}")
        except urllib.error.HTTPError as e:
            print(f"{k}: not taken: {json.loads(e.read() or b'{}').get('error', e.code)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
