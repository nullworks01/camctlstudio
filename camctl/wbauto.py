"""White balance without a grey card: find neutral references in the frame, then lock the Kelvin.

Local: the frame is split into cells; bright, unclipped, low-saturation cells are neutral
candidates (white walls, paper, grey surfaces). Coloured surfaces are rejected because they
disagree with the common cast of the neutral ones; clipped and very dark cells are ignored.
Agreement between several regions gives the confidence; two clusters with different casts mean
mixed light. Optionally Claude points out which objects are truly neutral (regions + light
sources); Studio measures those regions itself either way.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageStat

GX, GY = 12, 8


def _cells(im: Image.Image):
    w, h = im.size
    for gy in range(GY):
        for gx in range(GX):
            box = (gx * w // GX, gy * h // GY, (gx + 1) * w // GX, (gy + 1) * h // GY)
            yield [gx / GX, gy / GY, 1 / GX, 1 / GY], im.crop(box)


def _stats(c: Image.Image) -> dict:
    r, g, b = ImageStat.Stat(c).mean
    hist = c.convert("L").histogram()
    n = sum(hist) or 1
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    s = max(r, g, b)
    sat = (s - min(r, g, b)) / max(s, 1)
    return {"r": r, "g": g, "b": b, "ire": y * 100 / 255, "sat": sat,
            "clip": sum(hist[250:]) / n, "cast": (r - b) / max(r + b, 1), "tint": (g - (r + b) / 2) / max(g, 1)}


def find_neutrals(jpeg: bytes, to709=None, face_box=None) -> dict:
    """Neutral regions + their common cast and a confidence: {regions, cast, confidence, mixed, reason}."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((384, 384))
    if to709 is not None:
        im = to709(im)
    cands = []
    for box, c in _cells(im):
        if face_box and _overlap(box, face_box) > 0.3:      # skin is not a neutral
            continue
        s = _stats(c)
        if 30 <= s["ire"] <= 93 and s["clip"] < 0.02 and s["sat"] < 0.28 and abs(s["tint"]) < 0.12:
            cands.append({"box": box, **s})
    if len(cands) < 2:
        return {"regions": [], "cast": None, "confidence": "low", "mixed": False,
                "reason": "No white or grey surface found in the picture."}
    # keep the least saturated half, then the cells that agree with their median cast
    cands.sort(key=lambda c: c["sat"])
    pool = cands[:max(3, len(cands) // 2)]
    med = sorted(c["cast"] for c in pool)[len(pool) // 2]
    agree = [c for c in pool if abs(c["cast"] - med) < 0.035]
    far = [c for c in cands if abs(c["cast"] - med) > 0.08]                # neutral-ish cells under another light
    mixed = len(far) >= 3 and len(far) >= len(agree) * 0.5
    spread = math.sqrt(sum((c["cast"] - med) ** 2 for c in agree) / max(1, len(agree)))
    conf = "high" if len(agree) >= 6 and spread < 0.015 and not mixed else "medium" if len(agree) >= 3 and not mixed else "low"
    reason = ("Mixed light: two different light colours in the picture (e.g. window and lamp)." if mixed else
              f"{len(agree)} neutral area(s) agree." if conf != "low" else "Only a few neutral areas, they don't agree well.")
    return {"regions": [c["box"] for c in agree[:12]], "cast": round(sum(c["cast"] for c in agree) / len(agree), 4),
            "confidence": conf, "mixed": mixed, "reason": reason}


def regions_cast(jpeg: bytes, regions: list, to709=None) -> float | None:
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((384, 384))
    if to709 is not None:
        im = to709(im)
    w, h = im.size
    vals = []
    for x, y, bw, bh in regions:
        c = im.crop((int(x * w), int(y * h), max(int(x * w) + 2, int((x + bw) * w)), max(int(y * h) + 2, int((y + bh) * h))))
        s = _stats(c)
        if s["clip"] < 0.05 and s["ire"] > 15:
            vals.append(s["cast"])
    return sum(vals) / len(vals) if vals else None


def _overlap(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ix * iy / max(aw * ah, 1e-6)
