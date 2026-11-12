"""Exposure measurements on a live-view frame, in IRE (0-100), for the Director.

Pure Pillow (no numpy/OpenCV on this PC). The frame is measured as the camera
encodes it: with S-Log3 mid grey sits near 41 IRE and +1.5 stops near 55 IRE.
Face exposure uses OpenCV face detection when it is installed; otherwise a
skin-colour heuristic (YCbCr skin range) that also works on flat log footage.
"""
from __future__ import annotations

import io
import threading

from PIL import Image

try:   # imported once at startup: a first import inside a busy worker thread can take minutes
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except ImportError:
    cv2 = np = None

W = 256
# false-colour bands, the same as the Studio monitor
BANDS = [("crushed (<2.5)", 0, 2.5), ("very dark (2.5-10)", 2.5, 10), ("mid grey (38-46)", 38, 46),
         ("skin (52-58)", 52, 58), ("near clip (93-98)", 93, 98), ("clipped (>=98)", 98, 101)]


def _load(jpeg: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    h = max(1, round(im.height * W / im.width))
    return im.resize((W, h), Image.BILINEAR)


def _load_w(jpeg: bytes, width: int) -> Image.Image:
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.BILINEAR)


def _ire(values: list[int]) -> list[float]:
    return [v * 100 / 255 for v in values]


def _stats(ires: list[float]) -> dict:
    if not ires:
        return {"mean": None}
    s = sorted(ires)
    n = len(s)
    return {"mean": round(sum(s) / n, 1), "p10": round(s[n // 10], 1), "p50": round(s[n // 2], 1),
            "p90": round(s[min(n - 1, n * 9 // 10)], 1), "max": round(s[-1], 1),
            "clip_pct": round(100 * sum(1 for v in s if v >= 98) / n, 1)}


def _faces_cv(im: Image.Image) -> list[tuple[int, int, int, int]] | None:
    """Face boxes in im pixels, or None when OpenCV is not available."""
    global _CASCADE
    if cv2 is None or not hasattr(cv2, "CascadeClassifier"):
        return None
    with _CV_LOCK:                           # the cascade is not thread-safe (coach + director)
        if _CASCADE is None:
            cv2.setNumThreads(1)
            _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        return _detect(cv2, np, im)


def _detect(cv2, np, im):
    big = im if im.width >= 480 else im.resize((512, round(im.height * 512 / im.width)), Image.BILINEAR)
    k = im.width / big.width
    g = cv2.equalizeHist(cv2.cvtColor(np.array(big), cv2.COLOR_RGB2GRAY))   # also finds faces in flat log footage
    found = list(_CASCADE.detectMultiScale(g, 1.1, 4, minSize=(36, 36)))
    if not found:                            # turned or looking down: profile faces, both directions
        global _PROFILE
        if _PROFILE is None:
            _PROFILE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        found = list(_PROFILE.detectMultiScale(g, 1.1, 4, minSize=(36, 36)))
        if not found:
            gw = g.shape[1]
            found = [(gw - x - fw, y, fw, fh) for x, y, fw, fh in _PROFILE.detectMultiScale(cv2.flip(g, 1), 1.1, 4, minSize=(36, 36))]
    if not found:
        found = list(_CASCADE.detectMultiScale(g, 1.05, 3, minSize=(48, 48)))
    return [tuple(int(v * k) for v in f) for f in found]


_CASCADE = None
_PROFILE = None
_CV_LOCK = threading.Lock()


SKIN_GX, SKIN_GY = 32, 18


def _skin_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    """Box around the densest connected skin-coloured area (a face, not a beige wall).

    Skin pixels are counted per grid cell; the region grows from the densest cell over
    neighbouring cells that are at least half as dense, so large weakly skin-toned areas
    (walls, wood) don't join the face."""
    ycc = im.convert("YCbCr")
    w, h = ycc.size
    px = ycc.load()
    cw, ch = w / SKIN_GX, h / SKIN_GY
    dens = [[0.0] * SKIN_GX for _ in range(SKIN_GY)]
    for y in range(h):
        gy = min(SKIN_GY - 1, int(y / ch))
        row = dens[gy]
        for x in range(w):
            Y, cb, cr = px[x, y]
            if 77 <= cb <= 127 and 137 <= cr <= 173 and Y > 30:
                row[min(SKIN_GX - 1, int(x / cw))] += 1
    area = cw * ch
    best = max(((dens[gy][gx] / area, gx, gy) for gy in range(SKIN_GY) for gx in range(SKIN_GX)))
    peak, sx, sy = best
    if peak < 0.35:
        return None
    thr = max(0.25, peak * 0.5)
    seen, stack, cells = {(sx, sy)}, [(sx, sy)], []
    while stack:
        gx, gy = stack.pop()
        cells.append((gx, gy))
        for nx, ny in ((gx + 1, gy), (gx - 1, gy), (gx, gy + 1), (gx, gy - 1)):
            if 0 <= nx < SKIN_GX and 0 <= ny < SKIN_GY and (nx, ny) not in seen and dens[ny][nx] / area >= thr:
                seen.add((nx, ny))
                stack.append((nx, ny))
    if len(cells) > SKIN_GX * SKIN_GY * 0.35:      # half the frame "is skin": not a face
        return None
    x0, x1 = min(c[0] for c in cells), max(c[0] for c in cells) + 1
    y0, y1 = min(c[1] for c in cells), max(c[1] for c in cells) + 1
    return int(x0 * cw), int(y0 * ch), max(1, int((x1 - x0) * cw)), max(1, int((y1 - y0) * ch))


_LOG_LUT = None


def _to_709(im: Image.Image) -> Image.Image:
    """S-Log3.Cine -> Rec.709, so skin gets its colour back for detection (log is very desaturated)."""
    global _LOG_LUT
    if _LOG_LUT is None:
        from PIL import ImageFilter
        from .grade import log_to_rec709
        _LOG_LUT = ImageFilter.Color3DLUT.generate(17, lambda r, g, b: log_to_rec709(r, g, b))
    return im.filter(_LOG_LUT)


def analyze(jpeg: bytes, log: bool = False) -> dict:
    im = _load(jpeg)
    w, h = im.size
    luma = list(im.convert("L").tobytes())
    ires = _ire(luma)
    out = {"overall": _stats(ires)}
    n = len(ires)
    out["false_colour_pct"] = {name: round(100 * sum(1 for v in ires if lo <= v < hi) / n, 1)
                               for name, lo, hi in BANDS}
    # centre-weighted: middle third counts 3x
    cw, tot = 0.0, 0.0
    for i, v in enumerate(ires):
        x, y = i % w, i // w
        wt = 3.0 if (w / 3 <= x < 2 * w / 3 and h / 3 <= y < 2 * h / 3) else 1.0
        cw += v * wt
        tot += wt
    out["centre_weighted_ire"] = round(cw / tot, 1)
    zones = {}
    names = [["top-left", "top", "top-right"], ["left", "centre", "right"], ["bottom-left", "bottom", "bottom-right"]]
    for zy in range(3):
        for zx in range(3):
            vals = [ires[y * w + x] for y in range(zy * h // 3, (zy + 1) * h // 3)
                    for x in range(zx * w // 3, (zx + 1) * w // 3)]
            st = _stats(vals)
            zones[names[zy][zx]] = {"mean": st["mean"], "clip_pct": st["clip_pct"]}
    out["zones"] = zones
    faces = _faces_cv(_load_w(jpeg, 512))
    if faces:
        k = w / 512
        faces = [tuple(int(v * k) for v in f) for f in faces]
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        out["face"] = {"method": "face detection", **region_stats(im, x / w, y / h, fw / w, fh / h)}
    elif faces is None:                      # no OpenCV: skin-colour heuristic
        box = _skin_box(_to_709(im) if log else im)
        if box:
            x, y, bw, bh = box
            out["face"] = {"method": "skin-colour heuristic", **region_stats(im, x / w, y / h, bw / w, bh / h)}
    return out


def region_stats(im_or_jpeg, x: float, y: float, rw: float, rh: float) -> dict:
    im = _load(im_or_jpeg) if isinstance(im_or_jpeg, (bytes, bytearray)) else im_or_jpeg
    w, h = im.size
    x0, y0 = int(min(max(x, 0), 1) * w), int(min(max(y, 0), 1) * h)
    x1, y1 = max(x0 + 1, int(min(max(x + rw, 0), 1) * w)), max(y0 + 1, int(min(max(y + rh, 0), 1) * h))
    crop = im.crop((x0, y0, x1, y1)).convert("L")
    st = _stats(_ire(list(crop.tobytes())))
    st["box"] = [round(x0 / w, 2), round(y0 / h, 2), round((x1 - x0) / w, 2), round((y1 - y0) / h, 2)]
    return st


def summary(m: dict, log: bool) -> str:
    o = m["overall"]
    lines = [f"Overall: mean {o['mean']} IRE (p10 {o['p10']}, median {o['p50']}, p90 {o['p90']}, max {o['max']}), "
             f"{o['clip_pct']}% clipped. Centre-weighted {m['centre_weighted_ire']} IRE."]
    f = m.get("face")
    if f:
        lines.append(f"Face/skin ({f['method']}, box x,y,w,h={f['box']}): mean {f['mean']} IRE, brightest skin p90 "
                     f"{f['p90']} IRE, {f['clip_pct']}% clipped.")
    else:
        lines.append("No face/skin area found (use measure_region on the subject).")
    lines.append("Zones mean IRE / clipped%: " + "; ".join(f"{k} {v['mean']}/{v['clip_pct']}%" for k, v in m["zones"].items()))
    lines.append("False-colour coverage: " + ", ".join(f"{k} {v}%" for k, v in m["false_colour_pct"].items()))
    lines.append("Targets: " + ("S-Log3: mid grey 41 IRE, skin about 50-60 IRE (+1 to +2 stops over grey), keep "
                                "important highlights under 90 IRE." if log else
                                "Rec.709: skin 55-70 IRE (lighter skin toward 70), mid grey about 45 IRE, "
                                "keep important highlights under 98 IRE."))
    return "\n".join(lines)
