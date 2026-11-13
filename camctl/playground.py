"""Playground experiments (More › Playground). Each is off until he switches it on; every photo is his own press;
results are NEW files under <shoot>\\PLAYGROUND\\<experiment>\\; originals and the card never change.

- Double exposure: two of his photos blended at full size ("Soft" = average, "Bright" = screen, Bright by default).
- Long exposure: a proposed setup (10 / 20 / 30 s, lowest ISO, about f/8, focus and white balance locked), only on
  his Apply; one real photo by his press. Practice: the live-picture version, made in the page, saved here.
- Shoot for a mood: one Claude call per press (assistant.mood); the proposal is applied only on his Apply.
- Focus pull: two focus positions from his taps, moved between in small even steps by his press. Never records.
No Claude call anywhere except Shoot for a mood.
"""
from __future__ import annotations

import io
import math
import os
import time
from pathlib import Path

NAMES = {"double": "Double exposure", "paint": "Paint with light", "mood": "Shoot for a mood", "pull": "Focus pull"}


def out_dir(shoot: Path, key: str) -> Path:
    d = Path(shoot) / "PLAYGROUND" / NAMES[key]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_jpeg(im, out: Path, exif: bytes | None = None, quality: int = 95) -> Path:
    tmp = out.with_suffix(".part")
    kw = {"quality": quality, "subsampling": 0}
    if exif:
        kw["exif"] = exif
    im.save(tmp, "JPEG", **kw)
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- double exposure
BLENDS = {"bright": "Bright", "soft": "Soft"}


def blend(first: Path, second: Path, how: str = "bright"):
    """The two camera JPEGs at full size (the second fitted to the first's size), as an RGB image."""
    from PIL import Image, ImageChops
    a = Image.open(first).convert("RGB")
    b = Image.open(second).convert("RGB")
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    return ImageChops.screen(a, b) if how == "bright" else Image.blend(a, b, 0.5)


def double_save(first: Path, second: Path, shoot: Path, how: str = "bright") -> Path:
    from PIL import Image
    exif = Image.open(first).info.get("exif")
    out = out_dir(shoot, "double") / f"{Path(first).stem} + {Path(second).stem} - {BLENDS[how]}.jpg"
    return _save_jpeg(blend(first, second, how), out, exif)


def preview(path: Path, width: int = 960) -> bytes:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((width, width))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


# ---------------------------------------------------------------- long exposure
LONG_TIMES = (10, 20, 30)


def long_offered(shutter_choices: list[int], shutter_seconds) -> dict[int, int]:
    """{seconds: camera value} for the long times the camera itself lists (nothing invented, never Bulb)."""
    out = {}
    for v in shutter_choices or []:
        s = shutter_seconds(v)
        if s and any(abs(s - t) < 0.01 for t in LONG_TIMES):
            out[int(round(s))] = v
    return dict(sorted(out.items()))


def long_setup(props, seconds: int, codes: dict, shutter_seconds) -> list[dict]:
    """The proposal: the chosen long shutter, the lowest ISO, about f/8, focus MF, white balance Kelvin (as it is)."""
    ch = []
    sh = props.get(codes["shutter_speed"])
    offered = long_offered(list(sh.choices or []) if sh else [], shutter_seconds)
    if seconds not in offered:
        return []
    ch.append({"name": "shutter_speed", "value": offered[seconds]})
    iso = props.get(codes["iso"])
    isos = sorted(c for c in (iso.choices or []) if 0 < c < 0xFFFFFF) if iso else []
    if isos:
        ch.append({"name": "iso", "value": isos[0]})
    ap = props.get(codes["aperture"])
    aps = [c for c in (ap.choices or []) if c] if ap else []
    if aps:
        ch.append({"name": "aperture", "value": min(aps, key=lambda v: abs(v / 100 - 8.0))})
    fm = props.get(codes["focus_mode"])
    if fm is not None and 1 in (fm.choices or []):
        ch.append({"name": "focus_mode", "value": 1})
    wb = props.get(codes["white_balance"])
    if wb is not None and 32786 in (wb.choices or []):
        ch.append({"name": "white_balance", "value": 32786})
        ct = props.get(codes["color_temp"])
        if ct is not None and isinstance(ct.value, int) and ct.value > 0:
            ch.append({"name": "color_temp", "value": ct.value})
    return ch


# ---------------------------------------------------------------- focus pull
def pull_plan(a: int, b: int, seconds: float, max_steps: int = 40) -> tuple[list[int], float]:
    """Small even steps from a to b: (steps, seconds between them)."""
    d = b - a
    if d == 0:
        return [], 0.0
    size = max(1, math.ceil(abs(d) / max_steps))
    n = math.ceil(abs(d) / size)
    steps, left = [], abs(d)
    for _ in range(n):
        s = min(size, left)
        steps.append(s if d > 0 else -s)
        left -= s
    return steps, max(0.02, seconds / n)


# ---------------------------------------------------------------- paint with light: the practice picture
def save_practice(jpeg: bytes, shoot: Path) -> Path:
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    out = out_dir(shoot, "paint") / time.strftime("Practice %Y-%m-%d %H%M%S.jpg")
    return _save_jpeg(im, out)
