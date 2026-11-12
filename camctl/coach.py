"""Live coach: plain-language checks on the current live-view frame, with one-tap fixes.

Runs locally on the newest frame the live-view pump already has (no extra camera
traffic, no Claude calls). Checks: exposure of the face/subject, clipped areas
(located on the image), focus on the subject, noise (ISO) and battery.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageFilter, ImageStat

from . import measure, presets

LOG_PROFILES = (7, 8, 9)
GRID_X, GRID_Y = 16, 9


def _level_target(log: bool, face: bool) -> float:
    """Target brightness (0-255) for the face, or for the whole frame without a face."""
    if log:
        return presets.SLOG3_GREY + presets.SLOG3_STOP * (1.5 if face else 1.0)
    return 158.0 if face else 118.0            # skin ~62 IRE / average ~46 IRE in Rec.709


def stops_off(level: float, log: bool, face: bool) -> float:
    """How many stops brighter (+) the subject should be."""
    target = _level_target(log, face)
    if log:
        return (target - level) / presets.SLOG3_STOP
    return math.log2(presets._lin(target) / presets._lin(max(level, 1.0)))


def _clip_cells(im: Image.Image) -> list[list[float]]:
    """Grid cells (x, y, w, h in 0..1) where more than a quarter of the pixels clip."""
    g = im.convert("L")
    w, h = g.size
    cells = []
    for gy in range(GRID_Y):
        for gx in range(GRID_X):
            box = (gx * w // GRID_X, gy * h // GRID_Y, (gx + 1) * w // GRID_X, (gy + 1) * h // GRID_Y)
            hist = g.crop(box).histogram()
            n = sum(hist) or 1
            if sum(hist[250:]) / n > 0.25:
                cells.append([gx / GRID_X, gy / GRID_Y, 1 / GRID_X, 1 / GRID_Y])
    return cells


def _sharpness(im: Image.Image, box: list[float] | None) -> tuple[float, float]:
    """Edge energy inside the subject box and in the whole frame."""
    g = im.convert("L").filter(ImageFilter.FIND_EDGES)
    w, h = g.size
    whole = ImageStat.Stat(g).mean[0]
    if not box:
        box = [0.3, 0.25, 0.4, 0.5]
    x, y, bw, bh = box
    crop = g.crop((int(x * w), int(y * h), max(int(x * w) + 2, int((x + bw) * w)), max(int(y * h) + 2, int((y + bh) * h))))
    return ImageStat.Stat(crop).mean[0], whole


def analyze(jpeg: bytes, settings: dict, status: dict) -> dict:
    """settings: {name: value} of the main controls; status: battery etc.
    Returns {checks: [...], overlays: {...}, line: str}."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((measure.W, measure.W))
    log = settings.get("picture_profile") in LOG_PROFILES
    m = measure.analyze(jpeg, log=log)
    face = m.get("face")
    if face and face.get("mean") is not None:
        subj_box, subj_level, has_face = face["box"], face["mean"] * 2.55, True
    else:
        subj_box, subj_level, has_face = None, m["centre_weighted_ire"] * 2.55, False
    checks = []

    # exposure
    err = stops_off(subj_level, log, has_face)
    clip = m["overall"]["clip_pct"]
    face_clip = (face or {}).get("clip_pct", 0) or 0
    who = "Your face" if has_face else "The picture"
    if face_clip > 3:
        st, txt, fix_err = "bad", f"{who} has blown-out highlights", -max(0.7, face_clip / 15)
    elif abs(err) <= 0.5:
        st, txt, fix_err = "ok", f"{who} is well exposed", 0.0
    elif abs(err) <= 1.2:
        st, txt, fix_err = "warn", f"{who} is a bit {'dark' if err > 0 else 'bright'} ({err:+.1f} stops)", err
    else:
        st, txt, fix_err = "bad", f"{who} is too {'dark' if err > 0 else 'bright'} ({err:+.1f} stops)", err
    tgt = round(_level_target(log, has_face) / 2.55)
    if log:          # in S-Log3 numbers: where the skin (or the frame) sits against the target
        txt += f" · {'skin' if has_face else 'frame'} {subj_level / 2.55:.0f} IRE, target {tgt}"
    # real per-channel clipping on the frame as received (log clips near 94 IRE, Rec.709 near 98)
    lim = 240 if log else 250
    px = im.tobytes()
    n = max(1, len(px) // 3)
    rgb_clip = [round(100 * sum(1 for v in px[c::3] if v >= lim) / n, 2) for c in range(3)]
    if any(v > 0.5 for v in rgb_clip) and st == "ok":
        st = "warn"
        txt += " · clipping " + "/".join(f"{k} {v:.1f}%" for k, v in zip("RGB", rgb_clip) if v > 0.5)
    checks.append({"key": "exposure", "status": st, "label": txt, "target_ire": tgt, "rgb_clip": rgb_clip,
                   "ire": round(subj_level / 2.55), "stops": round(err, 2),
                   "fix": {"kind": "exposure", "stops": round(fix_err, 2), "label": "Fix exposure"} if st != "ok" else None})

    # clipping elsewhere in the frame
    cells = _clip_cells(im)
    if clip > 2 and face_clip <= 3:
        checks.append({"key": "clipping", "status": "warn" if clip < 8 else "bad",
                       "label": f"{clip:.0f}% of the frame is pure white (marked on the picture)",
                       "fix": {"kind": "exposure", "stops": -0.7, "label": "Protect highlights"}})

    # focus
    sub, whole = _sharpness(im, subj_box)
    ratio = sub / whole if whole else 0
    fst = "ok" if ratio >= 0.9 else "warn"
    checks.append({"key": "focus", "status": fst,
                   "label": ("Sharp on " + ("your face" if has_face else "the subject")) if fst == "ok"
                   else ("Soft on " + ("your face" if has_face else "the subject") + ", refocus?"),
                   "ratio": round(ratio, 2), "fix_disabled": {"kind": "focus", "label": "Focus here",
                           "x": round((subj_box[0] + subj_box[2] / 2) if subj_box else 0.5, 3),
                           "y": round((subj_box[1] + subj_box[3] / 2) if subj_box else 0.5, 3)} if fst != "ok" else None,
                   "fix": None})          # no AF from the coach: refocusing is a tap, the focus button or an Operator proposal

    # noise (from ISO)
    iso = settings.get("iso")
    if iso and 0 < iso < 0xFFFFFF:
        from .capture import NOISE_GUARD, ISO_CEILING
        md = "video" if settings.get("exposure_mode") in (0x78050, 0x78051, 0x78052, 0x78053, 0x98059, 0x9805A, 0x9805B, 0x9805C) else "photo"
        nst = "ok" if iso <= NOISE_GUARD[md] else "warn" if iso <= ISO_CEILING[md] else "bad"
        checks.append({"key": "noise", "status": nst,
                       "label": f"ISO {iso}: " + {"ok": "clean", "warn": "visible grain (the price of correct exposure)", "bad": "very grainy"}[nst]
                       + ("" if nst == "ok" else ". More light on you is the real fix"),
                       "fix": None})
    else:
        checks.append({"key": "noise", "status": "ok", "label": "Auto ISO", "fix": None})

    batt = status.get("battery")
    if batt is not None:
        checks.append({"key": "battery", "status": "ok" if batt > 25 else "warn" if batt > 10 else "bad",
                       "label": f"Battery {batt}%", "fix": None})

    directions = _directions(im, m, subj_box, has_face, log)

    order = {"bad": 0, "warn": 1, "ok": 2}
    worst = sorted(checks, key=lambda c: order[c["status"]])[0]
    line = "Looking good: ready to shoot." if worst["status"] == "ok" else worst["label"] + "."
    return {"checks": checks, "line": line, "log": log, "directions": directions,
            "overlays": {"subject": subj_box, "subject_is_face": has_face, "clip_cells": cells}}


def _half_means(im: Image.Image, box: list[float]) -> tuple[float, float]:
    g = im.convert("L")
    w, h = g.size
    x, y, bw, bh = box
    x0, y0, x1, y1 = int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)
    mid = (x0 + x1) // 2
    left = ImageStat.Stat(g.crop((x0, y0, max(x0 + 1, mid), max(y0 + 1, y1)))).mean[0]
    right = ImageStat.Stat(g.crop((mid, y0, max(mid + 1, x1), max(y0 + 1, y1)))).mean[0]
    return left * 100 / 255, right * 100 / 255


def _directions(im: Image.Image, m: dict, box, has_face: bool, log: bool) -> list[dict]:
    """Plain-language directions from the picture itself (light direction, backlight, framing).
    'light' is where the light comes from as seen from the camera: left / right / behind / front."""
    out = []
    if not has_face or not box:
        if m["overall"]["mean"] is not None:
            out.append({"key": "face", "text": "I can't see your face: look toward the camera.", "light": None})
        return out
    face = m["face"]["mean"] or 0
    x, y, bw, bh = box
    # backlight: the area around/behind the subject much brighter than the face
    around = [v["mean"] for k, v in m["zones"].items() if k in ("top", "top-left", "top-right", "left", "right") and v["mean"] is not None]
    if around and max(around) - face > (18 if log else 25) and m["overall"]["clip_pct"] > 1:
        out.append({"key": "backlight", "light": "behind",
                    "text": "You're backlit by a bright light behind you. Turn about 45 degrees toward it, or close the curtain behind you."})
    left, right = _half_means(im, box)
    diff = left - right
    if abs(diff) > (8 if log else 12):
        side = "left" if diff > 0 else "right"          # brighter side of the face, as the camera sees it
        dark = "right" if side == "left" else "left"
        out.append({"key": "side", "light": side,
                    "text": f"The light comes from the {side}. For softer shadows, put a white board or reflector on the {dark} side."})
    if face < (35 if log else 40):
        out.append({"key": "dark", "light": "front",
                    "text": "Your face needs more light. Face the window, or put a lamp in front of you, a bit to one side."})
    eyes = y + bh * 0.4
    if eyes > 0.45:
        out.append({"key": "eyes_low", "light": None, "text": "Raise the camera a little: your eyes should sit near the top third of the frame."})
    elif eyes < 0.22:
        out.append({"key": "eyes_high", "light": None, "text": "Tilt the camera down a little: there's too little room above your head."})
    if y < 0.02:
        out.append({"key": "headroom", "light": None, "text": "Your head touches the top edge: step back a little or zoom out."})
    cx = x + bw / 2
    if abs(cx - 0.5) > 0.3:
        out.append({"key": "center", "light": None,
                    "text": "Move a bit toward the " + ("right" if cx < 0.5 else "left") + " so you're not at the edge of the frame."})
    return out[:3]


def plan_exposure_fix(props, stops: float, max_iso: int = 6400, lock_iris: bool = False) -> list[tuple[str, int]]:
    """Setting changes (ISO first, then aperture) that move exposure by about `stops`."""
    from .followlight import plan_step
    from .drivers.sony import PROPS
    changes: list[tuple[str, int]] = []
    remaining = stops
    props = dict(props)
    for _ in range(3):
        if abs(remaining) < 0.3:
            break
        name, value = plan_step(props, remaining, False, max_iso, lock_iris)
        if name is None:
            break
        p = props[PROPS[name]]
        before = p.value
        if name == "iso":
            moved = math.log2(value / before)
        else:
            moved = -2 * math.log2(value / before)
        changes.append((name, value))
        remaining -= moved
        props[PROPS[name]] = type(p)(**{**p.__dict__, "value": value})
    return changes
