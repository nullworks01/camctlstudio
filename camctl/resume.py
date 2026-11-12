"""Picking a stopped shoot up again: what a saved setup needs besides the camera values.

All local (no Claude): the outline of the saved frame for lining up, how much the light changed since, a distance
estimate from a face (or an object whose size he entered), and one plain line-up instruction at a time.
"""
from __future__ import annotations

import base64
import io
import math

import numpy as np
from PIL import Image

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

FACE_WIDTH_M = 0.17          # width of the face box the detector draws on an adult face (it reaches past the
                             # cheekbones): checked 12 Nov 2026 against his measured 0.8 m at 16 mm (0.15 read 15-31 % short)
DISTANCE_ERROR = "about ±20%"


def _im(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


def outline_png(jpeg: bytes, max_w: int = 1024) -> bytes:
    """The saved frame's edges as fine lines: white with a thin dark edge (reads on any picture), the rest transparent."""
    im = _im(jpeg)
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    g = np.asarray(im.convert("L"))
    if cv2 is not None:
        g2 = cv2.GaussianBlur(g, (0, 0), 1.6)
        gx, gy = cv2.Sobel(g2, cv2.CV_32F, 1, 0), cv2.Sobel(g2, cv2.CV_32F, 0, 1)
        mag = np.abs(gx) + np.abs(gy)
        hi = float(max(12.0, np.percentile(mag, 96)))          # the strongest few percent of edges, also soft ones
        e = cv2.Canny(g2, hi * 0.4, hi)
        e = cv2.dilate(e, np.ones((2, 2), np.uint8))
        halo = cv2.dilate(e, np.ones((4, 4), np.uint8))
    else:                                     # without OpenCV: a plain gradient threshold
        gy, gx = np.gradient(g.astype(np.float32))
        e = ((np.hypot(gx, gy) > 28) * 255).astype(np.uint8)
        halo = e
    rgba = np.zeros((g.shape[0], g.shape[1], 4), np.uint8)
    rgba[halo > 0] = (10, 12, 16, 150)        # the thin dark edge
    rgba[e > 0] = (255, 255, 255, 235)        # the white line
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def light_level(jpeg: bytes) -> float:
    """Mean lightness L* of the picture (for 'is the light the same as when it was saved?')."""
    im = _im(jpeg)
    im.thumbnail((320, 320))
    a = np.asarray(im).astype(np.float32) / 255.0
    lin = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
    y = float((lin @ np.array([0.2126, 0.7152, 0.0722])).mean())
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


def stops_between(level_saved: float, level_now: float) -> float:
    """Exposure difference in stops between two L* levels taken with the same camera values."""
    def Y(L):
        L = max(float(L), 2.0)
        return ((L + 16) / 116) ** 3 if L > 8 else L / 903.3
    return math.log2(Y(level_now) / Y(level_saved))


def light_sentence(stops: float) -> str:
    """One plain sentence when the light is clearly different (more than about one stop), else ''."""
    if abs(stops) < 1.0:
        return ""
    return (f"The light is about {abs(stops):.1f} stop{'s' if abs(stops) >= 1.5 else ''} "
            f"{'brighter' if stops > 0 else 'darker'} than when you saved this setup. Nothing was changed for that.")


def distance_m(size_frac: float, focal_mm: float | None, sensor_w_mm: float | None, real_m: float) -> float | None:
    """Distance from how wide something of known size is in the picture (fraction of the picture width)."""
    if not focal_mm or not sensor_w_mm or not size_frac or size_frac <= 0 or real_m <= 0:
        return None
    on_sensor_mm = size_frac * sensor_w_mm
    return focal_mm * real_m / on_sensor_mm


def estimate(jpeg: bytes, focal_mm: float | None, sensor_w_mm: float | None, faces_fn=None,
             object_cm: float | None = None, object_box: list | None = None) -> dict:
    """{'m', 'method', 'focal_mm', 'error'} or {'m': None, 'why'} ('Distance not known' instead of a guess)."""
    if not focal_mm or not sensor_w_mm:
        return {"m": None, "why": "No focal length known for this lens."}
    if object_cm and object_box and len(object_box) == 4 and object_box[2] > 0:
        m = distance_m(float(object_box[2]), focal_mm, sensor_w_mm, float(object_cm) / 100)
        return {"m": round(m, 2), "method": f"object {float(object_cm):g} cm wide, marked on the picture",
                "focal_mm": round(focal_mm, 1), "error": "about ±10% if the box is drawn tight"}
    im = _im(jpeg)
    faces = faces_fn(im) if faces_fn else None
    if faces:
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        m = distance_m(w / im.width, focal_mm, sensor_w_mm, FACE_WIDTH_M)
        return {"m": round(m, 2), "method": "face size (average face)", "focal_mm": round(focal_mm, 1),
                "error": DISTANCE_ERROR, "face_frac": round(w / im.width, 3)}
    return {"m": None, "why": "No face found and no object size entered."}


def line_up_say(res: dict) -> tuple[str, bool]:
    """One plain instruction from a Match comparison, and whether it is lined up."""
    raw, sc = res.get("raw") or {}, res.get("scores") or {}
    s0, dx0, dy0 = raw.get("scale", 1.0) or 1.0, raw.get("dx", 0.0), raw.get("dy", 0.0)
    # lined up = the camera is in the same place: position and size match (a person moving in the frame lowers the
    # picture similarity, not the geometry; measured 12 Nov 2026)
    if raw.get("cc", 0) >= 0.4 and abs(dx0) < 0.025 and abs(dy0) < 0.025 and abs(math.log2(s0)) < 0.035:
        return "Lined up", True
    if sc.get("framing", 0) >= 92:
        return "Lined up", True
    if raw.get("cc", 0) < 0.15 and sc.get("framing", 0) < 30:
        return "Too different to compare: point the camera at the same place first.", False
    s, dx, dy = raw.get("scale", 1.0) or 1.0, raw.get("dx", 0.0), raw.get("dy", 0.0)
    if abs(math.log2(s)) >= 0.06 and abs(math.log2(s)) >= 2 * max(abs(dx), abs(dy)):
        return ("Further back" if s > 1 else "Move closer"), False
    if abs(dx) >= abs(dy):
        return ("A little right" if dx > 0 else "A little left"), False
    return ("Camera lower" if dy > 0 else "Camera higher"), False


def b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data else None


def jpeg_q(jpeg: bytes, max_w: int, q: int) -> bytes:
    im = _im(jpeg)
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q)
    return buf.getvalue()
