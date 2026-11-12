"""Match this shot: how close is the live picture to a reference picture?

Local and cheap (OpenCV on small copies, ~10 ms), no Claude call. Four numbers 0..100 and one plain sentence:

- framing:    where the reference's content sits in the live picture (phase correlation + ECC on edges):
              shift left/right/up/down and size (closer / farther)
- brightness: difference of the mid-tones in stops
- contrast:   ratio of the tonal range in stops (p95 vs p5 luminance): the same whatever the exposure
- colour:     warmer / cooler (Lab b*) and green / magenta (Lab a*) of the mean colour

An S-Log3 live picture is compared after a Rec.709 view conversion (the reference is a finished picture),
so a flat log monitor never reads as "too little contrast". The file on the card is never touched.
"""
from __future__ import annotations

import math

import numpy as np

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

W = 320                       # working width; the reference is centre-cropped to the live aspect


def _decode(jpeg: bytes) -> np.ndarray:
    im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError("cannot read the picture")
    return im


_LOG_LUT = None


def _log_lut() -> np.ndarray:
    global _LOG_LUT
    if _LOG_LUT is None:
        from .grade import log_to_rec709
        _LOG_LUT = np.array([round(log_to_rec709(v / 255, v / 255, v / 255)[0] * 255) for v in range(256)], np.uint8)
    return _LOG_LUT


def _crop_to(im: np.ndarray, aspect: float) -> np.ndarray:
    h, w = im.shape[:2]
    if w / h > aspect:                       # too wide: cut the sides
        nw = int(round(h * aspect))
        x = (w - nw) // 2
        return im[:, x:x + nw]
    nh = int(round(w / aspect))
    y = (h - nh) // 2
    return im[y:y + nh]


def prepare(ref_jpeg: bytes) -> np.ndarray:
    """Decode a reference once (kept small)."""
    if cv2 is None:
        raise RuntimeError("OpenCV is not available")
    im = _decode(ref_jpeg)
    k = 960 / max(im.shape[:2])
    return cv2.resize(im, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else im


def _small(im: np.ndarray, aspect: float) -> np.ndarray:
    im = _crop_to(im, aspect)
    return cv2.resize(im, (W, int(round(W / aspect))), interpolation=cv2.INTER_AREA)


def _framing(ref: np.ndarray, live: np.ndarray) -> dict:
    """Similarity of the layout: where the reference's centre lands in the live picture, and how big."""
    def edges(im):
        g = cv2.GaussianBlur(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), (0, 0), 2.0).astype(np.float32)
        gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
        m = cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 3.0)
        return m / (float(m.max()) + 1e-6)
    a, b = edges(ref), edges(live)
    h, w = a.shape
    # start from a translation guess (phase correlation copes with big shifts), refine with ECC; keep the best
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), _ = cv2.phaseCorrelate(a, b, win)
    best = None
    cx, cy = w / 2, h / 2
    # coarse search at half size (sizes x shift guesses, few iterations), then one refinement at full size
    a2, b2 = cv2.pyrDown(a), cv2.pyrDown(b)
    starts = [(s, dx, dy) for s in (1.0, 0.8, 1.25) for dx, dy in ((0.0, 0.0), (sx, sy))]
    for s, dx, dy in starts:
        warp = np.array([[s, 0, (cx * (1 - s) + dx) / 2], [0, s, (cy * (1 - s) + dy) / 2]], np.float32)
        try:
            cc, warp = cv2.findTransformECC(a2, b2, warp, cv2.MOTION_AFFINE,
                                            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, 1e-3), None, 3)
        except cv2.error:
            continue
        if best is None or cc > best[0]:
            best = (cc, warp)
    if best is not None:
        warp = best[1].copy()
        warp[:, 2] *= 2
        try:
            best = cv2.findTransformECC(a, b, warp, cv2.MOTION_AFFINE,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 1e-4), None, 5)
        except cv2.error:
            best = (best[0], warp)
    if best is None:
        cc = float(np.corrcoef(a.ravel(), b.ravel())[0, 1])
        return {"ok": False, "cc": max(0.0, cc), "dx": 0.0, "dy": 0.0, "scale": 1.0}
    cc, warp = best
    A = warp[:, :2].astype(np.float64)
    s = math.sqrt(abs(np.linalg.det(A)))
    c = np.array([w / 2, h / 2])
    p = A @ c + warp[:, 2]                    # where the reference's centre is in the live picture
    if not (0.4 < s < 2.5):                   # nonsense fit: treat as "different picture"
        return {"ok": False, "cc": max(0.0, float(cc)) * 0.5, "dx": 0.0, "dy": 0.0, "scale": 1.0}
    return {"ok": True, "cc": float(cc), "dx": float((p[0] - c[0]) / w), "dy": float((p[1] - c[1]) / h), "scale": float(s)}


def _Y(L: float) -> float:
    """CIE L* -> relative luminance (floored so black does not become -infinity stops)."""
    L = max(float(L), 2.0)
    return ((L + 16) / 116) ** 3 if L > 8 else L / 903.3


def _lab_stats(im: np.ndarray) -> dict:
    lab = cv2.cvtColor(im, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = lab[..., 0] * (100 / 255)
    a, b = lab[..., 1] - 128, lab[..., 2] - 128
    lo, mid, hi = np.percentile(L, (5, 50, 95))
    spread = math.log2(_Y(hi) / _Y(lo))      # tonal range in stops between the dark and bright parts
    return {"L": float(mid), "Lmean": float(L.mean()), "spread": spread, "a": float(a.mean()), "b": float(b.mean())}


def _stops(L_ref: float, L_live: float) -> float:
    return math.log2(_Y(L_live) / _Y(L_ref))


def compare(ref: np.ndarray, live_jpeg: bytes, log: bool = False) -> dict:
    if cv2 is None:
        raise RuntimeError("OpenCV is not available")
    live = _decode(live_jpeg)
    if log:
        live = cv2.LUT(live, _log_lut())
    aspect = live.shape[1] / live.shape[0]
    r, l = _small(ref, aspect), _small(live, aspect)

    fr = _framing(r, l)
    off = math.hypot(fr["dx"], fr["dy"])
    size = abs(math.log2(fr["scale"]))
    framing = 100 * max(0.0, min(1.0, fr["cc"])) ** 0.5 * max(0.0, 1 - 2.2 * off - 1.4 * size) if fr["ok"] \
        else 40 * max(0.0, min(1.0, fr["cc"]))

    sr, sl = _lab_stats(r), _lab_stats(l)
    stops = _stops(sr["Lmean"], sl["Lmean"])
    brightness = max(0.0, 100 - 45 * abs(stops))
    cratio = (sl["spread"] + 0.3) / (sr["spread"] + 0.3)
    contrast = max(0.0, 100 - 120 * abs(math.log2(cratio)))
    warm = sl["b"] - sr["b"]                 # + = live is warmer (more yellow)
    tint = sl["a"] - sr["a"]                 # + = live is more magenta
    colour = max(0.0, 100 - 4.0 * math.hypot(warm, tint))

    scores = {"framing": round(framing), "brightness": round(brightness), "contrast": round(contrast), "colour": round(colour)}
    detail = {
        "framing": ("same" if framing >= 85 else "a different picture" if not fr["ok"] else _where(fr)),
        "brightness": "same" if abs(stops) < 0.2 else f"{abs(stops):.1f} stop {'brighter' if stops > 0 else 'darker'}",
        "contrast": "same" if abs(math.log2(cratio)) < 0.12 else ("more contrast" if cratio > 1 else "flatter"),
        "colour": "same" if math.hypot(warm, tint) < 3 else
                  " and ".join(x for x in (("warmer" if warm > 0 else "cooler") if abs(warm) >= 3 else "",
                                         ("more magenta" if tint > 0 else "greener") if abs(tint) >= 3 else "") if x)
                  or "slightly different",
    }
    overall = round(0.4 * framing + 0.25 * brightness + 0.15 * contrast + 0.2 * colour)
    return {"scores": scores, "detail": detail, "overall": overall, "say": _sentence(scores, fr, stops, cratio, warm, tint),
            "raw": {"dx": round(fr["dx"], 3), "dy": round(fr["dy"], 3), "scale": round(fr["scale"], 3), "cc": round(fr["cc"], 3),
                    "stops": round(stops, 2), "contrast_ratio": round(cratio, 2), "warm": round(warm, 1), "tint": round(tint, 1)},
            "log": log}


def _where(fr: dict) -> str:
    parts = []
    if abs(fr["dx"]) >= 0.04:
        parts.append("further right" if fr["dx"] > 0 else "further left")
    if abs(fr["dy"]) >= 0.04:
        parts.append("lower" if fr["dy"] > 0 else "higher")
    if abs(math.log2(fr["scale"])) >= 0.08:
        parts.append("bigger" if fr["scale"] > 1 else "smaller")
    return ", ".join(parts) if parts else "close"


def _sentence(sc: dict, fr: dict, stops: float, cratio: float, warm: float, tint: float) -> str:
    """One plain instruction for the biggest difference (what he can change, in his words)."""
    if min(sc.values()) >= 85:
        return "That's a match. Take the shot."
    worst = min(sc, key=lambda k: sc[k] * (0.8 if k == "framing" else 1.0))   # framing first when close
    if worst == "framing":
        if not fr["ok"]:
            return "The framing is too different to compare: line the ghost up with the picture first."
        s = fr["scale"]
        if abs(math.log2(s)) >= 0.08 and abs(math.log2(s)) >= 2 * max(abs(fr["dx"]), abs(fr["dy"])):
            pct = round(abs(s - 1) * 100)
            return (f"Step back or zoom out a little: everything is about {pct}% bigger than in the reference." if s > 1 else
                    f"Move closer or zoom in a little: everything is about {pct}% smaller than in the reference.")
        if abs(fr["dx"]) >= abs(fr["dy"]):
            return (f"Turn the camera a little to the {'right' if fr['dx'] > 0 else 'left'}: the picture sits "
                    f"{'right' if fr['dx'] > 0 else 'left'} of the reference.")
        return (f"Tilt the camera a little {'down' if fr['dy'] > 0 else 'up'}: the picture sits "
                f"{'low' if fr['dy'] > 0 else 'high'} compared to the reference.")
    if worst == "brightness":
        return (f"It's about {abs(stops):.1f} stop {'brighter' if stops > 0 else 'darker'} than the reference: "
                f"{'take light away from the subject (or lower the exposure)' if stops > 0 else 'add light on the subject (or raise the exposure)'}.")
    if worst == "contrast":
        return ("More contrast than the reference: add fill light on the shadow side or soften the key light." if cratio > 1 else
                "Flatter than the reference: take the fill light away or move the key light more to the side.")
    if abs(warm) >= abs(tint):
        k = round(min(2000, abs(warm) * 120) / 100) * 100 or 200
        return (f"Warmer than the reference: set the white balance about {k} K lower, or use cooler light."
                if warm > 0 else
                f"Cooler than the reference: set the white balance about {k} K higher, or use warmer light.")
    return ("More magenta than the reference: shift the white balance a little toward green." if tint > 0 else
            "Greener than the reference: shift the white balance a little toward magenta (often a fluorescent or LED light).")
