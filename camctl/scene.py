"""Scene analysis: what a cinematographer reads off the monitor before touching the camera.

Local and cheap (Pillow + optional OpenCV face detection), on the live-view frame:
subject type and size, light level, key direction and lit:shadow ratio, clipped areas,
colour of the light and mixed light, flicker banding, background separation, movement,
and the lens/camera limits from the property choices. `decide()` turns it into a shot type
and a cinematographer's notes; the camera part is done by the capture profiles.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageFilter, ImageStat

from . import coach, measure

SHOT_NAMES = {"interview": "talking head", "portrait": "portrait", "product": "product / object",
              "daylight": "bright exterior", "lowlight": "low light"}


def _lin(ire: float) -> float:
    return max((max(ire, 0.1) / 100) ** 2.2, 1e-4)


def _stops(a_ire: float, b_ire: float) -> float:
    return math.log2(_lin(a_ire) / _lin(b_ire))


def _region(im: Image.Image, box) -> Image.Image:
    w, h = im.size
    x, y, bw, bh = box
    return im.crop((int(x * w), int(y * h), max(int(x * w) + 2, int((x + bw) * w)), max(int(y * h) + 2, int((y + bh) * h))))


def _cast(im: Image.Image) -> float:
    r, _g, b = ImageStat.Stat(im).mean
    return (r - b) / max(r + b, 1)


def banding(im: Image.Image) -> float:
    """Strength of horizontal stripes (mains flicker with a mismatched shutter), 0..1."""
    g = im.convert("L").resize((64, 96))
    rows = [ImageStat.Stat(g.crop((0, y, 64, y + 1))).mean[0] for y in range(96)]
    smooth = [sum(rows[max(0, i - 6):i + 7]) / len(rows[max(0, i - 6):i + 7]) for i in range(96)]
    hp = [r - s for r, s in zip(rows, smooth)]
    sd = math.sqrt(sum(v * v for v in hp) / len(hp))
    flips = sum(1 for a, b in zip(hp, hp[1:]) if a * b < 0)
    return round(min(1.0, sd / 6.0) if 6 <= flips <= 40 else 0.0, 2)


def analyze(jpeg: bytes, log: bool, props=None, history: list | None = None) -> dict:
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((measure.W, measure.W))
    m = measure.analyze(jpeg, log=log)
    face = m.get("face") if (m.get("face") or {}).get("method") == "face detection" else None
    s = {"log": log, "overall_ire": m["overall"]["mean"], "clip_pct": m["overall"]["clip_pct"],
         "clip_zones": [k for k, v in m["zones"].items() if (v["clip_pct"] or 0) > 3]}
    edges = im.convert("L").filter(ImageFilter.FIND_EDGES)
    centre = ImageStat.Stat(_region(edges, (0.3, 0.25, 0.4, 0.5))).mean[0]
    border = (ImageStat.Stat(_region(edges, (0, 0, 1, 0.2))).mean[0] + ImageStat.Stat(_region(edges, (0, 0.8, 1, 0.2))).mean[0]) / 2
    if face:
        box = face["box"]
        h = box[3]
        s["subject"] = {"type": "person", "box": box, "ire": face["mean"],
                        "size": "close-up" if h > 0.45 else "medium" if h > 0.22 else "wide"}
        left, right = coach._half_means(im, box)
        lit, shadow = max(left, right), min(left, right)
        ratio = 2 ** max(0.0, _stops(lit, shadow))
        s["key"] = {"side": "left" if left > right else "right", "ratio": round(ratio, 1),
                    "ratio_text": f"{max(1, round(ratio))}:1"}
        bg = [v["mean"] for k, v in m["zones"].items() if k in ("top-left", "top-right", "left", "right") and v["mean"] is not None]
        bgm = sum(bg) / len(bg) if bg else s["overall_ire"]
        s["background"] = {"ire": round(bgm, 1), "separation_stops": round(_stops(face["mean"], bgm), 1)}
        s["backlit"] = bool(bg and max(bg) - face["mean"] > (18 if log else 25) and m["overall"]["clip_pct"] > 1)
        fc = _cast(_region(im, box))
        bc = _cast(_region(im, (0, 0, 1, 0.25)))
        s["light_colour"] = {"face": round(fc, 3), "background": round(bc, 3),
                             "mixed": abs(fc - bc) > 0.07,
                             "warmth": "warm (tungsten)" if fc > 0.12 else "cool (daylight/window)" if fc < -0.02 else "neutral"}
    else:
        product = centre > border * 1.35 and centre > 8
        s["subject"] = {"type": "object" if product else "scene", "box": [0.3, 0.25, 0.4, 0.5] if product else None,
                        "ire": m["centre_weighted_ire"], "size": "close" if product else "wide"}
        s["backlit"] = False
        left = ImageStat.Stat(_region(im.convert("L"), (0, 0, 0.5, 1))).mean[0] * 100 / 255
        right = ImageStat.Stat(_region(im.convert("L"), (0.5, 0, 0.5, 1))).mean[0] * 100 / 255
        s["key"] = {"side": "left" if left > right else "right", "ratio": round(2 ** abs(_stops(left, right)), 1),
                    "ratio_text": f"{max(1, round(2 ** abs(_stops(left, right))))}:1"}
        s["background"] = None
        lc = _cast(_region(im, (0, 0, 0.5, 1)))
        rc = _cast(_region(im, (0.5, 0, 0.5, 1)))
        s["light_colour"] = {"face": None, "background": round((lc + rc) / 2, 3), "mixed": abs(lc - rc) > 0.08,
                             "warmth": "warm (tungsten)" if (lc + rc) / 2 > 0.12 else "cool (daylight/window)" if (lc + rc) / 2 < -0.02 else "neutral"}
    box = s["subject"].get("box") or [0.4, 0.35, 0.2, 0.3]
    sb, whole = coach._sharpness(im, box)
    s["subject"]["sharp"] = round(sb / whole, 2) if whole else None      # focus check on the target
    s["flicker"] = banding(im)
    s["moving"] = False
    if history:
        prev = [h["subject"].get("box") for h in history[-3:] if h.get("subject", {}).get("box")]
        cur = s["subject"].get("box")
        if prev and cur:
            d = max(abs((p[0] + p[2] / 2) - (cur[0] + cur[2] / 2)) for p in prev)
            s["moving"] = d > 0.06
    if props is not None:
        from .drivers.sony import PROPS
        aps = sorted(c for c in (props.get(PROPS["aperture"]).choices if props.get(PROPS["aperture"]) else []) if c)
        isos = sorted(c for c in (props.get(PROPS["iso"]).choices if props.get(PROPS["iso"]) else []) if 0 < c < 0xFFFFFF)
        s["limits"] = {"widest_f": aps[0] / 100 if aps else None, "narrowest_f": aps[-1] / 100 if aps else None,
                       "base_iso": isos[0] if isos else None, "max_iso": isos[-1] if isos else None}
    return s


def decide(s: dict, mode: str) -> dict:
    """Shot type + cinematographer's notes (what to do with light / position before the camera)."""
    subj = s["subject"]
    level = subj.get("ire") or s["overall_ire"] or 0
    dark = level < (30 if s["log"] else 35)
    bright = s["clip_pct"] > 8 and not subj.get("type") == "person" or (s["overall_ire"] or 0) > 75
    if subj["type"] == "person":
        shot = "lowlight" if dark else "daylight" if bright else ("interview" if mode == "video" or subj["size"] != "close-up" else "portrait")
    elif subj["type"] == "object":
        shot = "product"
    else:
        shot = "daylight" if bright else "lowlight" if dark else "daylight"
    why, todo = [], []
    if subj["type"] == "person":
        why.append(f"{subj['size'].capitalize()} of a person, key from the {s['key']['side']} at about {s['key']['ratio_text']}.")
        if s.get("backlit"):
            todo.append({"key": "backlight", "light": "behind", "text": "Strong light behind you: turn 45 degrees toward the window or close the curtain behind you."})
        if s["key"]["ratio"] > 6:
            dark_side = "right" if s["key"]["side"] == "left" else "left"
            todo.append({"key": "ratio", "light": s["key"]["side"],
                         "text": f"Contrast is very hard ({s['key']['ratio_text']}): add a white board or bounce on the {dark_side} side."})
        elif s["key"]["ratio"] < 1.4 and not dark:
            todo.append({"key": "flat", "light": "front", "text": "The light is flat: move the key light (or yourself) so it comes 30–45 degrees from one side."})
        bg = s.get("background") or {}
        if bg and bg.get("separation_stops", 0) < 0.4 and not s.get("backlit"):
            todo.append({"key": "separation", "light": None, "text": "You blend into the background: more distance from the wall, or a small light on the background."})
    elif subj["type"] == "object":
        why.append("Object in the centre: everything on it must be sharp, so a smaller aperture and a steady camera.")
    else:
        why.append("Wide scene without a clear subject.")
    if s.get("clip_zones"):
        why.append("Highlights clip in the " + ", ".join(s["clip_zones"][:2]) + ".")
    if s["light_colour"].get("mixed"):
        todo.append({"key": "mixed", "light": None, "text": "Mixed light (window and lamp): switch one off or gel the lamp, so the white balance can be right everywhere."})
    if s.get("flicker", 0) > 0.3:
        why.append("Mains-light flicker bands visible: shutter set to a multiple of the mains frequency.")
    if dark:
        todo.append({"key": "dark", "light": "front", "text": "Not enough light for a clean image: add a soft light in front, 45 degrees to one side, or move toward the window."})
    if s.get("moving"):
        why.append("The subject is moving: continuous eye-tracking focus.")
    return {"shot": shot, "shot_name": SHOT_NAMES[shot], "why": why[:4], "directions": todo[:3]}
