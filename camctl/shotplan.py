"""Claude plans the shoot (shot list) and reviews every take (with continuity checks).

Shot list: NOTES\\shot_list.json in the shoot folder. One Claude call writes it (or a
built-in template without an API key); each shot can be "Set up" (one call, or a
built-in preset), and is ticked off when a take is recorded/shot for it.

Take review: local measurements first (coach checks on a frame of the take), plus
continuity with the earlier takes of the shoot (face exposure, white balance), with
one-tap fixes. Claude reviews only when asked (or with automatic review turned on).
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageStat

from . import assistant, handoff

TEMPLATES = {
    "video": [
        {"name": "Opening hook", "kind": "video", "framing": "Medium close-up, eyes on the upper third", "lens": "wide open for a soft background",
         "lighting": "Soft key 45° to one side, slightly above eye level", "action": "Look into the lens, say your first line with energy."},
        {"name": "Main talk", "kind": "video", "framing": "Medium shot, chest up, room for gestures", "lens": "wide open or one stop closed",
         "lighting": "Same key; add a bounce on the shadow side", "action": "Deliver the main points; pause between them for easy cuts."},
        {"name": "Detail / B-roll", "kind": "video", "framing": "Close-ups of hands, product or screen", "lens": "f/4–f/5.6 for more depth",
         "lighting": "Key from behind-side for texture", "action": "Slow, steady movements of 5–10 s each."},
        {"name": "Closing", "kind": "video", "framing": "Medium close-up", "lens": "wide open",
         "lighting": "As opening", "action": "Call to action, smile, hold 2 s of silence at the end."},
    ],
    "photo": [
        {"name": "Hero portrait", "kind": "photo", "framing": "Head and shoulders, eyes on the upper third", "lens": "wide open for background blur",
         "lighting": "Soft key 45° high, fill from a reflector", "action": "Chin slightly down, eyes to the lens."},
        {"name": "Wider environmental", "kind": "photo", "framing": "Half body with the space around you", "lens": "f/5.6",
         "lighting": "Key plus room light", "action": "Natural pose, interact with something in the room."},
        {"name": "Detail", "kind": "photo", "framing": "Tight on hands / object", "lens": "f/4",
         "lighting": "Side light for texture", "action": "Hold still for 2 s."},
    ],
}

PLAN_SYSTEM = (
    "You are a director of photography planning a shoot on a Sony ZV-E10 (APS-C, kit zoom 16-50 mm f/3.5-5.6 "
    "unless the person says otherwise). From the description, write a practical shot list a solo creator can "
    "shoot in order. Each shot: a short name, framing, lens/aperture intent, a lighting note, and what to say "
    "or do. 3 to 10 shots. Short and concrete; no filler."
)


def _plan_schema() -> dict:
    shot = {"type": "object", "additionalProperties": False,
            "required": ["name", "kind", "framing", "lens", "lighting", "action"],
            "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["video", "photo"]},
                           "framing": {"type": "string"}, "lens": {"type": "string"},
                           "lighting": {"type": "string"}, "action": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["title", "shots"],
            "properties": {"title": {"type": "string"}, "shots": {"type": "array", "items": shot}}}


def _file(sh: dict | None = None) -> Path:
    sh = sh or handoff.ensure_shoot()
    return Path(sh["path"]) / "NOTES" / "shot_list.json"


def load(sh: dict | None = None) -> dict:
    try:
        return json.loads(_file(sh).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"title": "", "shots": [], "current": None}


def save(plan: dict) -> dict:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    md = [f"# Shot list: {plan.get('title', '')}", ""] + [
        f"{i + 1}. [{'x' if s.get('done') else ' '}] **{s['name']}** ({s['kind']}): {s['framing']}; {s['lens']}; "
        f"light: {s['lighting']}; do: {s['action']}" for i, s in enumerate(plan.get("shots", []))]
    (f.parent / "shot_list.md").write_text("\n".join(md), encoding="utf-8")
    return plan


def make_plan(description: str, mode: str) -> dict:
    """One Claude call, or the built-in template without a key."""
    if assistant.has_key():
        body = {"model": assistant.MODEL, "max_tokens": 6000, "system": assistant.cam(PLAN_SYSTEM),
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _plan_schema()}},
                "fallbacks": "default",
                "messages": [{"role": "user", "content": f"Main mode: {mode}. Shoot: {description or 'a talking-head video'}"}]}
        out = assistant._json_answer(assistant.call(body, assistant._key()))
        plan = {"title": out.get("title") or description[:60], "shots": out.get("shots", [])[:12], "by": "claude"}
    else:
        plan = {"title": description[:60] or ("Talking-head video" if mode == "video" else "Portrait session"),
                "shots": [dict(s) for s in TEMPLATES[mode]], "by": "template"}
    for s in plan["shots"]:
        s.update(done=False, takes=[])
    plan["current"] = 0 if plan["shots"] else None
    return save(plan)


def shot_request(s: dict) -> str:
    return (f"Set the camera up for this planned shot: {s['name']} ({s['kind']}). Framing: {s['framing']}. "
            f"Lens/aperture: {s['lens']}. Lighting: {s['lighting']}. It is shot for post-production "
            "(S-Log3 for video, RAW for photos): a technically perfect neutral capture, no looks or colour settings.")


def builtin_preset(s: dict) -> str:
    """Capture profile for a planned shot."""
    text = (s["name"] + " " + s["framing"] + " " + s["lens"] + " " + s["lighting"]).lower()
    if any(w in text for w in ("detail", "product", "hands", "object", "b-roll")):
        return "product"
    if any(w in text for w in ("outdoor", "exterior", "daylight", "sun")):
        return "daylight"
    if any(w in text for w in ("low light", "dark", "night")):
        return "lowlight"
    return "interview" if s["kind"] == "video" else "portrait"


def tick(take: dict) -> None:
    """Mark the current shot as shot when a take is logged for it."""
    plan = load()
    i = plan.get("current")
    if i is None or i >= len(plan["shots"]):
        return
    s = plan["shots"][i]
    if s["name"] != take.get("shot"):
        return
    s["done"] = True
    s.setdefault("takes", []).append(take["id"])
    save(plan)


# ---------------------------------------------------------------- take review
def colour_cast(jpeg: bytes, box: list[float] | None = None) -> float:
    """Warm (+) / cool (-) balance of the face or frame: (R-B)/(R+B)."""
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((256, 256))
    if box:
        w, h = im.size
        x, y, bw, bh = box
        im = im.crop((int(x * w), int(y * h), max(int(x * w) + 2, int((x + bw) * w)), max(int(y * h) + 2, int((y + bh) * h))))
    r, _g, b = ImageStat.Stat(im).mean
    return round((r - b) / max(r + b, 1), 3)


def local_review(analysis: dict, measure_now: dict, previous: list[dict]) -> dict:
    """Good points, fixes (with one-tap fix where a setting can do it) and continuity."""
    good, fixes = [], []
    for c in analysis.get("checks", []):
        if c["key"] == "battery":
            continue
        (good if c["status"] == "ok" else fixes).append({"text": c["label"], "fix": c.get("fix")})
    cont = None
    prev = next((t for t in reversed(previous) if t.get("measure", {}).get("ire") is not None), None)
    if prev and measure_now.get("ire") is not None:
        d_ire = measure_now["ire"] - prev["measure"]["ire"]
        d_cast = (measure_now.get("cast") or 0) - (prev["measure"].get("cast") or 0)
        notes = []
        if abs(d_ire) >= 4:
            notes.append(f"face is {abs(d_ire):.0f} IRE {'brighter' if d_ire > 0 else 'darker'} than take {prev['id']}")
        if abs(d_cast) >= 0.03:
            notes.append(f"colour is {'warmer' if d_cast > 0 else 'cooler'} than take {prev['id']}")
        sv, pv = measure_now.get("values", {}), prev.get("measure", {}).get("values", {})
        if pv.get("white_balance") != sv.get("white_balance") or pv.get("color_temp") != sv.get("color_temp"):
            notes.append("white balance setting differs")
        cont = {"previous": prev["id"], "matches": not notes, "notes": notes}
    return {"good": good[:3], "fixes": fixes[:2], "continuity": cont}


def match_changes(prev: dict, now_values: dict) -> list[dict]:
    """Settings that make this take match an earlier one (white balance; exposure is done separately)."""
    pv = prev.get("measure", {}).get("values", {})
    ch = []
    for n in ("white_balance", "color_temp"):             # technical match only; colour stays neutral
        if n in pv and pv[n] != now_values.get(n):
            ch.append({"name": n, "value": pv[n]})
    return ch


REVIEW_SYSTEM = (
    "You are a camera technician reviewing one take from a Sony ZV-E10 shoot for a professional who grades it "
    "himself, in the format given below (with a log profile a flat picture is expected and correct). Judge "
    "TECHNICAL quality only: "
    "exposure for the format, clipped highlights, noise/ISO, white balance accuracy, focus on the eyes, framing. "
    "Never comment on style or suggest looks. In two or three short points say what is good, then the one or two "
    "most important technical things to fix for the next take (usually light or framing). "
    "Only suggest camera setting changes from the allowed values. Short, concrete, professional."
)


def claude_review(frame: bytes, settings: list[dict], measurements: str, shot: str) -> dict:
    lines = [f"- {s['name']}: {s['value']} ({s['label']}). Allowed: " + ", ".join(f"{c['value']} ({c['label']})" for c in s["choices"])
             for s in settings if s.get("writable") and s.get("choices")]
    schema = {"type": "object", "additionalProperties": False, "required": ["good", "fix", "settings"],
              "properties": {"good": {"type": "array", "items": {"type": "string"}},
                             "fix": {"type": "array", "items": {"type": "string"}},
                             "settings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                     "required": ["name", "value"],
                                                                     "properties": {"name": {"type": "string"}, "value": {"type": "integer"}}}}}}
    body = {"model": assistant.MODEL, "max_tokens": 6000, "system": assistant.cam(REVIEW_SYSTEM),
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}, "fallbacks": "default",
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(frame).decode()}},
                {"type": "text", "text": f"Shot: {shot or 'unnamed'}.\nMeasurements: {measurements}\nSettings:\n" + "\n".join(lines)}]}]}
    out = assistant._json_answer(assistant.call(body, assistant._key()))
    changes, dropped = assistant._validate_settings(out.get("settings", []), settings)
    return {"good": out.get("good", [])[:3], "fix": out.get("fix", [])[:3],
            "changes": [c for c in changes if not c["same"]], "dropped": dropped}
