"""Saved setups (shot list) and 'My gear', stored in %APPDATA%\\camctl.

A setup is one JSON file: name, notes, mode, every camera setting (as the Director
sees them), preset/look, an optional Lighting Lab plan, reference images and a
thumbnail of the live view (images downscaled, base64 JPEG).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import uuid
from pathlib import Path

BASE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl"
SETUP_DIR = BASE / "setups"
GEAR_FILE = BASE / "gear.json"


def _shrink(b64: str, max_side: int) -> str:
    """Downscale a base64 JPEG/PNG (data URL or bare) to max_side pixels, JPEG q85."""
    from PIL import Image
    data = base64.b64decode(str(b64).split(",", 1)[-1])
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _path(sid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", sid or ""):
        raise KeyError(sid)
    return SETUP_DIR / f"{sid}.json"


EXTRA_KEYS = ("shot", "shot_name", "zoom", "shoot", "frame", "outline", "light", "light_measure", "distance",
              "measure", "object", "focus", "camera", "nd", "formats")


def _clean_measure(m: dict | None) -> dict:
    """His own measurements in metres (empty fields left out)."""
    def num(v):
        try:
            v = float(v)
            return round(v, 2) if 0 < v < 100 else None
        except (TypeError, ValueError):
            return None
    m = m or {}
    out = {k: num(m.get(k)) for k in ("subject_m", "height_m")}
    lights = []
    for li in (m.get("lights") or [])[:8]:
        e = {"name": str(li.get("name", ""))[:40], "dist_m": num(li.get("dist_m")), "height_m": num(li.get("height_m"))}
        if e["name"] or e["dist_m"] or e["height_m"]:
            lights.append(e)
    out["lights"] = lights
    return {k: v for k, v in out.items() if v}


def save(name: str, notes: str, mode: str, settings: list[dict], preset: str | None, look: str | None,
         plan: dict | None, refs: list[str], thumb: bytes | None, extra: dict | None = None) -> dict:
    """A setup holds everything needed to pick a stopped shoot up again. Never the Claude key or anything secret."""
    SETUP_DIR.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:12]
    d = {"id": sid, "v": 2, "name": (name or "Untitled setup").strip()[:80], "notes": (notes or "")[:2000],
         "mode": mode, "created": time.time(),
         "settings": [{"name": s["name"], "value": s["value"], "label": s["label"],
                       "friendly": s.get("friendly", "")} for s in settings if s.get("writable")],
         "preset": preset, "look": look, "plan": plan,
         "refs": [_shrink(r, 900) for r in (refs or [])[:4]],
         "thumb": base64.b64encode(thumb).decode() if thumb else None}
    if thumb:
        d["thumb"] = _shrink(d["thumb"], 640)
    for k, v in (extra or {}).items():
        if k in EXTRA_KEYS:
            d[k] = _clean_measure(v) if k == "measure" else v
    _path(sid).write_text(json.dumps(d), encoding="utf-8")
    return d


def update(sid: str, **fields) -> dict:
    """Change name, notes or his measurements of a saved setup."""
    d = get(sid)
    if "name" in fields:
        d["name"] = (fields["name"] or "Untitled setup").strip()[:80]
    if "notes" in fields:
        d["notes"] = (fields["notes"] or "")[:2000]
    if "measure" in fields:
        d["measure"] = _clean_measure(fields["measure"])
    if "object" in fields:
        d["object"] = fields["object"]
    if "distance" in fields:
        d["distance"] = fields["distance"]
    _path(sid).write_text(json.dumps(d), encoding="utf-8")
    return d


def get(sid: str) -> dict:
    return json.loads(_path(sid).read_text(encoding="utf-8"))


def list_all() -> list[dict]:
    out = []
    if SETUP_DIR.exists():
        for f in SETUP_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({k: d.get(k) for k in ("id", "name", "notes", "mode", "created", "preset", "look", "thumb", "shot_name")}
                       | {"has_plan": bool(d.get("plan")), "refs": len(d.get("refs") or []),
                          "zoom_mm": (d.get("zoom") or {}).get("mm"), "shoot": (d.get("shoot") or {}).get("name"),
                          "format": (d.get("formats") or {}).get("name"),
                          "key": {s["name"]: s["label"] for s in d.get("settings", [])
                                  if s["name"] in ("aperture", "shutter_speed", "iso", "white_balance", "color_temp",
                                                   "picture_profile")}})
    return sorted(out, key=lambda d: -(d.get("created") or 0))


def delete(sid: str) -> None:
    _path(sid).unlink(missing_ok=True)


def recall_changes(d: dict, order: list[str]) -> list[dict]:
    """The setup's settings as {name, value} changes, mode first (it changes what the rest allow)."""
    ch = [{"name": s["name"], "value": s["value"]} for s in d.get("settings", [])]
    return sorted(ch, key=lambda c: order.index(c["name"]) if c["name"] in order else 999)


def get_gear() -> str:
    try:
        return json.loads(GEAR_FILE.read_text(encoding="utf-8")).get("gear", "")
    except (OSError, ValueError):
        return ""


def set_gear(text: str) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    GEAR_FILE.write_text(json.dumps({"gear": (text or "")[:4000], "updated": time.time()}), encoding="utf-8")
