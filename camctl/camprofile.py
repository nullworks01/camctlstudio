"""Camera profiles: what Studio knows about a camera model, instead of assuming a ZV-E10.

- The ZV-E10 profile holds the values measured on the owner's camera (11-12 Nov 2026). Its behaviour is unchanged.
- Any other Sony starts as an *untested model*: everything that can be read from the camera is read (properties,
  allowed values, ranges, model name); what cannot be read uses the ZV-E10 numbers as *labelled defaults*; and
  nothing model-specific is ever written blindly (above all: no picture profile is assumed to be log).
- "Check this camera" (webui) tests a model without shooting and saves what worked here, per model, in
  %APPDATA%\\camctl\\camera_profiles.json. A log profile is only used after the owner confirmed it once.
Nothing in Studio claims support for a model that was never connected.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl"
SAVED = BASE / "camera_profiles.json"

# values that cannot be read from a camera; the ZV-E10's measured numbers are the defaults for other models
DEFAULTS = {
    "s1_settle_s": 3.2,                         # half press held before the shutter (measured on the ZV-E10)
    "noise_guard": {"photo": 3200, "video": 6400},
    "iso_ceiling": {"photo": 12800, "video": 12800},
    "battery_pct_per_h": {"wifi": 50, "usb": 30},     # Wi-Fi + live view measured 12 Nov 2026: about 48-52 %/h
    "focus_indicator": False,                   # 0xD213 does not change over the PC link on the ZV-E10
}

BUILTIN = {
    "ZV-E10": {
        "tested": True,
        "log_pp": 8, "log_pps": [7, 8, 9], "log_kind": "slog3", "log_confirmed": True,   # PP8 = S-Log3 (camera default)
        "log_kinds": {7: "slog2", 8: "slog3", 9: "slog3"},
        "log_min_iso": 500,
        "sensor_w_mm": 23.5,                       # APS-C (Sony spec), for the distance estimate
        "movie_m": 0x78053, "photo_m": 1,
        "still_raw_jpeg": 2,
        "lens": {"name": "E PZ 16-50 mm F3.5-5.6 (kit)", "zoom_mm": [(0, 16.0), (16, 18.4), (24, 21.5), (52, 31.9),
                                                                    (67, 36.6), (80, 42.5), (100, 50.0)]},
        "measured": ["s1_settle_s", "noise_guard", "iso_ceiling", "battery_pct_per_h", "focus_indicator", "zoom_mm"],
        **DEFAULTS,
    },
}


def _load_saved() -> dict:
    try:
        return json.loads(SAVED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_result(model: str, **changes) -> dict:
    """Store what a check found (or the owner confirmed) for this model."""
    data = _load_saved()
    cur = data.get(model, {})
    cur.update(changes, updated=time.time())
    data[model] = cur
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = SAVED.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, SAVED)
    return cur


def model_key(name: str | None) -> str:
    """'ZV-E10' from 'Sony ZV-E10', 'ZV-E10 (simulated)' etc."""
    n = (name or "").replace("Sony", "").strip()
    return n.split(" (")[0].strip() or "unknown"


def for_model(name: str | None) -> dict:
    """The profile for a connected camera: built-in values, then what checks saved, then labelled defaults."""
    key = model_key(name)
    base = BUILTIN.get(key)
    saved = _load_saved().get(key, {})
    if base is not None:
        p = {**base, **{k: v for k, v in saved.items() if k in ("log_pp", "log_confirmed", "log_kind", "check")}}
        p.update(model=key, builtin=True)
        return p
    p = {**DEFAULTS, "tested": False, "log_pp": None, "log_pps": [], "log_kind": None, "log_confirmed": False,
         "log_min_iso": None, "movie_m": 0x78053, "photo_m": 1, "still_raw_jpeg": 2, "lens": None,
         "measured": [], "defaults": list(DEFAULTS), "model": key, "builtin": False}
    p.update({k: v for k, v in saved.items() if k != "updated"})
    if saved.get("log_pp") is not None and saved.get("log_confirmed"):
        p["log_pps"] = [saved["log_pp"]]
    return p


def is_log(profile: dict, pp_value) -> bool:
    """Is this picture profile the log profile? Only when the profile knows it (never guessed)."""
    if pp_value is None:
        return False
    if profile.get("builtin"):
        return pp_value in profile.get("log_pps", [])
    return bool(profile.get("log_confirmed")) and pp_value == profile.get("log_pp")


def log_kind(profile: dict, pp_value) -> str | None:
    """'slog3' / 'slog2' / 'log' (confirmed but the kind is not known) / None."""
    if not is_log(profile, pp_value):
        return None
    kinds = profile.get("log_kinds") or {}
    return kinds.get(pp_value) or kinds.get(str(pp_value)) or profile.get("log_kind") or "log"


def of(studio) -> dict:
    """The profile of the camera a Studio is connected to (cached per model name)."""
    cam = getattr(studio, "cam", None)
    name = getattr(cam, "name", None) or getattr(studio, "_profile_name", None)
    cached = getattr(studio, "_profile", None)
    if cached is None or cached.get("_name") != name:
        cached = {**for_model(name), "_name": name}
        try:
            studio._profile, studio._profile_name = cached, name
        except Exception:  # noqa: BLE001
            pass
        try:                                             # prompts and texts name what is connected
            from . import assistant
            assistant.CAMERA_NAME = f"Sony {cached.get('model')}" if name else "Sony camera"
            lens = (cached.get("lens") or {}).get("name")
            assistant.CAMERA_LENS = ("kit zoom 16-50 mm f/3.5-5.6" if cached.get("model") == "ZV-E10" else
                                     (lens or "the lens the camera reports"))
        except Exception:  # noqa: BLE001
            pass
    return cached


def camera_text(profile: dict, aperture_choices: list | None = None) -> str:
    """How prompts and texts name the camera: the model and lens it reports, never a fixed 'ZV-E10'."""
    name = f"Sony {profile.get('model', 'camera')}"
    lens = (profile.get("lens") or {}).get("name")
    if not lens and aperture_choices:
        fs = sorted(v / 100 for v in aperture_choices if v and v < 0xFFFF)
        if fs:
            lens = f"lens with apertures from f/{fs[0]:g}"
    return name + (f" ({lens})" if lens else "")


def looks_like_log(jpeg: bytes) -> dict:
    """Is the live picture flat like a log profile? (low contrast and saturation, mid-tones lifted)"""
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((320, 320))
    a = np.asarray(im).astype(np.float32)
    lum = a @ np.array([0.2126, 0.7152, 0.0722])
    sat = float(np.asarray(im.convert("HSV"))[..., 1].mean())
    p5, p95 = np.percentile(lum, (5, 95))
    flat = (p95 - p5) < 130 and sat < 30 and p5 > 25
    return {"log_like": bool(flat), "contrast": round(float(p95 - p5)), "saturation": round(sat, 1), "black": round(float(p5))}
