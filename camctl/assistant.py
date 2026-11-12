"""'Ask Claude' for camctl Studio: suggest camera settings from the live view.

Calls the Claude Messages API over plain HTTPS (Python stdlib). The official
`anthropic` SDK can't load on this PC because Windows Smart App Control blocks
one of its native modules, so this is the documented raw-HTTP fallback.

The API key is the one saved inside Studio (DPAPI, see keystore.py) or, as a fallback, the ANTHROPIC_API_KEY
environment variable, read on every call. It is never logged or sent anywhere except api.anthropic.com.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-5-5"

SYSTEM = (
    "You are an expert photographer operating a Sony ZV-E10 mirrorless camera remotely. "
    "You see the camera's current live-view frame and its current settings. Choose settings "
    "that give the best possible photo for what the user asks; if they ask nothing specific, "
    "make the most professional-looking shot of the scene. Think about exposure (the live view "
    "reflects the current settings), motion blur (handheld subjects need about 1/60 s or "
    "faster at this focal length), noise (keep ISO as low as the light allows), depth of field "
    "and white balance. Only use values from the allowed choices given for each setting, and "
    "only include settings you want to change. Keep the explanation to two or three short "
    "sentences in plain language; put practical advice for the person (light, framing, pose, "
    "lens zoom) in tips_for_user, also short."
)


# How prompts name the camera: set from the connected camera's profile (camprofile.of), never a fixed model.
CAMERA_NAME = "Sony ZV-E10"
CAMERA_LENS = "kit zoom 16-50 mm f/3.5-5.6"


def cam(text: str) -> str:
    """A prompt with the connected camera's model and lens in place of the ZV-E10 wording it was written with."""
    return text.replace("Sony ZV-E10", CAMERA_NAME).replace("kit zoom 16-50 mm f/3.5-5.6", CAMERA_LENS)


# When a Claude call fails because of the key, the account or the internet, the reason is said in one plain
# sentence (no codes, no technical words), shown where he pressed and again on the next start (Getting ready).
PROBLEM_TEXT = {
    "wrong": "Claude didn't accept the key. Add it again: paste the key and press Save.",
    "forbidden": "This key isn't allowed to use Claude. Check it at console.anthropic.com, then add it again.",
    "nocredit": "The Claude account has no credit. Add credit at console.anthropic.com, then try again.",
    "offline": "Studio couldn't reach Claude. Check the internet connection, then try again.",
}


def _problem_file():
    from . import keystore
    return keystore.BASE / "claude_problem.json"


def problem() -> dict | None:
    """The last key/account/internet problem of a Claude call ({kind, text, t}), or None."""
    try:
        p = json.loads(_problem_file().read_text(encoding="utf-8"))
        return p if p.get("kind") in PROBLEM_TEXT else None
    except (OSError, ValueError):
        return None


def set_problem(kind: str | None) -> None:
    f = _problem_file()
    try:
        if kind is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"kind": kind, "text": PROBLEM_TEXT[kind], "t": time.time()}), encoding="utf-8")
    except OSError:
        pass


def mark_ok() -> None:
    """Time of the last Claude answer that worked (for the key row)."""
    try:
        f = _problem_file().with_name("claude_last_ok.txt")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def last_ok() -> float | None:
    try:
        return float(_problem_file().with_name("claude_last_ok.txt").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _problem_kind(msg: str) -> str | None:
    m = msg.lower()
    if "(401)" in m or "rejected the api key" in m:
        return "wrong"
    if "credit" in m or "billing" in m or "error 402" in m:
        return "nocredit"
    if "(403)" in m:
        return "forbidden"
    if "couldn't reach" in m:
        return "offline"
    return None


class AskError(Exception):
    """A problem to show to the user as-is. Key, credit and internet problems become one plain sentence and are
    remembered until the next call that works."""

    def __init__(self, msg: str = ""):
        kind = _problem_kind(str(msg))
        if kind:
            set_problem(kind)
            msg = PROBLEM_TEXT[kind]
        super().__init__(msg)


def _key() -> str:
    """The key saved inside Studio (DPAPI, this Windows user) wins; otherwise ANTHROPIC_API_KEY from this
    process's environment, or, on Windows, from the user's saved environment variables (so a key saved after
    Studio started works without restarting it). Never logged, never returned to the page."""
    from . import keystore
    saved = keystore.load()
    if saved:
        return saved
    return _env_key()


def _env_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key or os.name != "nt":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]).strip()
    except OSError:
        return ""


def has_key() -> bool:
    return bool(_key())


def key_source() -> str:
    """'app' (saved in Studio), 'env' (Windows setting / environment) or '' (none). Never the key itself."""
    from . import keystore
    if keystore.load():
        return "app"
    return "env" if _env_key() else ""


def test_key(key: str) -> tuple[str, str]:
    """One tiny request to check a key. Returns (status, plain sentence); status: ok | wrong | nocredit |
    forbidden | offline | busy | error. The key never appears in the sentence."""
    body = {"model": MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "Hi"}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": key,
                                          "anthropic-version": "2026-10-21"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
        usage.record(data.get("usage") or {})
        return "ok", "Claude is connected."
    except urllib.error.HTTPError as e:
        try:
            msg = str(json.loads(e.read()).get("error", {}).get("message", "")).lower()
        except ValueError:
            msg = ""
        if e.code == 401:
            return "wrong", "Claude didn't accept this key. Check that you copied all of it, or make a new one."
        if "credit" in msg or "billing" in msg or e.code == 402:
            return "nocredit", "The key works, but the account has no credit. Add credit at console.anthropic.com."
        if e.code == 403:
            return "forbidden", "This key isn't allowed to use Claude. Check its permissions at console.anthropic.com."
        if e.code in (429, 529) or e.code >= 500:
            return "busy", "Claude is busy right now. The key is saved; try again in a minute."
        return "error", f"Claude answered with an error ({e.code}). Try again in a minute."
    except (urllib.error.URLError, TimeoutError, OSError):
        return "offline", "Studio couldn't reach Claude. Check the internet connection."


def _schema(setting_names: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["settings", "explanation", "tips_for_user"],
        "properties": {
            "settings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "value"],
                    "properties": {
                        "name": {"type": "string", "enum": setting_names},
                        "value": {"type": "integer"},
                    },
                },
            },
            "explanation": {"type": "string"},
            "tips_for_user": {"type": "string"},
        },
    }


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2026-10-21",
                 "anthropic-beta": "server-side-fallback-2026-07-01"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    usage.record(data.get("usage") or {})
    return data


# ---------------------------------------------------------------- usage meter (token counts only)
import contextvars as _cv
import threading as _th
from pathlib import Path as _Path

FEATURE = _cv.ContextVar("camctl_feature", default="other")


class _Usage:
    """Token counts the API returns, per day / feature and per shoot, in %APPDATA%\\camctl\\usage.json.
    Never stores anything else (no prompts, no key)."""

    def __init__(self):
        self.file = _Path(os.environ.get("APPDATA") or _Path.home()) / "camctl" / "usage.json"
        self.lock = _th.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"days": {}, "shoots": {}}

    def record(self, u: dict) -> None:
        set_problem(None)                       # a call that worked: no problem to show any more
        mark_ok()
        inp = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
            int(u.get("cache_creation_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        feat = FEATURE.get()
        try:
            from . import handoff
            sh = (handoff.current() or {}).get("name", "")
        except Exception:  # noqa: BLE001
            sh = ""
        with self.lock:
            d = self.load()
            day = d["days"].setdefault(time.strftime("%Y-%m-%d"), {})
            for bucket in (day.setdefault(feat, {}), d["shoots"].setdefault(sh or "(no shoot)", {})):
                bucket["calls"] = bucket.get("calls", 0) + 1
                bucket["input"] = bucket.get("input", 0) + inp
                bucket["output"] = bucket.get("output", 0) + out
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(d), encoding="utf-8")

    def today(self) -> dict:
        d = self.load()
        feats = d["days"].get(time.strftime("%Y-%m-%d"), {})
        tot = {"calls": sum(f.get("calls", 0) for f in feats.values()),
               "input": sum(f.get("input", 0) for f in feats.values()),
               "output": sum(f.get("output", 0) for f in feats.values())}
        return {"total": tot, "features": feats, "shoots": d.get("shoots", {})}


usage = _Usage()


class feature:
    """with assistant.feature("director"): ...  tags the calls made inside for the usage meter."""

    def __init__(self, name: str):
        self.name, self.tok = name, None

    def __enter__(self):
        self.tok = FEATURE.set(self.name)

    def __exit__(self, *a):
        FEATURE.reset(self.tok)


def ask(live_view_jpeg: bytes, settings: list[dict], request: str) -> dict:
    """settings: [{name, value, label, writable, choices: [{value, label}]}] as shown in Studio.

    Returns {"changes": [{name, value, label, from_label}], "explanation", "tips",
             "dropped": [str]} with every value checked against the camera's choices.
    """
    key = _key()
    if not key:
        raise AskError("NO_KEY")
    writable = [s for s in settings if s.get("writable") and s.get("choices")]
    lines = []
    for s in writable:
        opts = ", ".join(f"{c['value']} ({c['label']})" for c in s["choices"])
        lines.append(f"- {s['name']}: currently {s['value']} ({s['label']}). Allowed: {opts}")
    text = ("Current camera settings and the values allowed for each:\n" + "\n".join(lines) +
            "\n\nRequest: " + (request.strip() or "Make the best professional shot of this scene."))
    body = {
        "model": MODEL,
        "max_tokens": 16000,
        "system": cam(SYSTEM),
        "output_config": {"effort": "medium",
                          "format": {"type": "json_schema", "schema": _schema([s["name"] for s in writable])}},
        "fallbacks": "default",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(live_view_jpeg).decode()}},
            {"type": "text", "text": text},
        ]}],
    }

    data = None
    for attempt in (1, 2):
        try:
            data = _post(body, key)
            break
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            if e.code == 401:
                raise AskError("Claude rejected the API key (401). Check ANTHROPIC_API_KEY.") from e
            if e.code == 403:
                raise AskError(f"Your API key isn't allowed to do this (403). {msg}") from e
            if (e.code == 429 or e.code >= 500) and attempt == 1:
                time.sleep(3)
                continue
            raise AskError(f"Claude API error {e.code}: {msg or e.reason}") from e
        except urllib.error.URLError as e:
            if attempt == 1:
                time.sleep(2)
                continue
            raise AskError(f"Couldn't reach the Claude API: {e.reason}") from e

    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AskError("Claude declined this request. Try wording it differently.")
    if stop == "max_tokens":
        raise AskError("Claude's answer was cut off. Please try again.")
    text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    if text_block is None:
        raise AskError("Claude returned no answer. Please try again.")
    try:
        result = json.loads(text_block["text"])
    except ValueError as e:
        raise AskError("Claude's answer wasn't in the expected format. Please try again.") from e

    by_name = {s["name"]: s for s in writable}
    changes, dropped = [], []
    for item in result.get("settings", []):
        s = by_name.get(item.get("name"))
        choice = next((c for c in (s or {}).get("choices", []) if c["value"] == item.get("value")), None)
        if s is None or choice is None:
            dropped.append(f"{item.get('name')}={item.get('value')}")
            continue
        if choice["value"] != s["value"]:
            changes.append({"name": s["name"], "value": choice["value"],
                            "label": choice["label"], "from_label": s["label"]})
    return {"changes": changes, "explanation": result.get("explanation", ""),
            "tips": result.get("tips_for_user", ""), "dropped": dropped,
            "model": data.get("model", MODEL)}


PICK_SYSTEM = (
    "You are the autopilot of a Sony ZV-E10 camera. From the live-view frame, pick the one "
    "preset (and optionally one cinematic look) that will give the best result for this scene. "
    "The preset engine then measures the light and sets exposure, ISO, white balance, focus and "
    "picture profile itself, so you only choose the intent. Prefer normal colour presets over "
    "LOG/HLG unless the user asked for log. Keep the reason to one short sentence in plain "
    "language and the tip to one short practical sentence (light, framing or pose)."
)


def _pick_post(body: dict, key: str) -> dict:
    for attempt in (1, 2):
        try:
            return _post(body, key)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AskError("Claude rejected the API key (401). Check ANTHROPIC_API_KEY.") from e
            if (e.code == 429 or e.code >= 500) and attempt == 1:
                time.sleep(3)
                continue
            raise AskError(f"Claude API error {e.code}") from e
        except urllib.error.URLError as e:
            if attempt == 1:
                time.sleep(2)
                continue
            raise AskError(f"Couldn't reach the Claude API: {e.reason}") from e
    raise AskError("Claude API unavailable")


def pick_preset(live_view_jpeg: bytes, mode: str, presets: list[dict], looks: list[dict],
                scene: str, request: str = "") -> dict:
    """Ask Claude which preset (and look) suits the frame. Returns {preset, look, reason, tip}."""
    key = _key()
    if not key:
        raise AskError("NO_KEY")
    pkeys = [p["key"] for p in presets]
    lkeys = ["none"] + [l["key"] for l in looks]
    text = (f"Section: {mode}. Measured light: {scene}.\nPresets:\n" +
            "\n".join(f"- {p['key']}: {p['name']} ({p.get('blurb', '')})" for p in presets) +
            "\nLooks (or none):\n" + "\n".join(f"- {l['key']}: {l['name']} ({l.get('blurb', '')})" for l in looks) +
            "\n\nUser wish: " + (request.strip() or "best professional result for this scene"))
    schema = {"type": "object", "additionalProperties": False,
              "required": ["preset", "look", "reason", "tip"],
              "properties": {"preset": {"type": "string", "enum": pkeys},
                             "look": {"type": "string", "enum": lkeys},
                             "reason": {"type": "string"}, "tip": {"type": "string"}}}
    body = {"model": MODEL, "max_tokens": 4000, "system": cam(PICK_SYSTEM),
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            "fallbacks": "default",
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(live_view_jpeg).decode()}},
                {"type": "text", "text": text}]}]}
    data = _pick_post(body, key)
    if data.get("stop_reason") == "refusal":
        raise AskError("Claude declined this request.")
    block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    try:
        out = json.loads(block["text"]) if block else {}
    except ValueError as e:
        raise AskError("Claude's answer wasn't in the expected format.") from e
    if out.get("preset") not in pkeys:
        raise AskError("Claude returned no usable preset.")
    look = out.get("look")
    return {"preset": out["preset"], "look": None if look in (None, "none") or look not in lkeys else look,
            "reason": out.get("reason", ""), "tip": out.get("tip", "")}


# ---------------------------------------------------------------- shared call helper
def call(body: dict, key: str) -> dict:
    """One Messages API call with retries and plain-language errors."""
    for attempt in (1, 2, 3):
        try:
            return _post(body, key)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            if e.code == 401:
                raise AskError("Claude rejected the API key (401). Check ANTHROPIC_API_KEY.") from e
            if e.code == 403:
                raise AskError(f"Your API key isn't allowed to do this (403). {msg}") from e
            if (e.code == 429 or e.code >= 500) and attempt < 3:
                time.sleep(3 * attempt)
                continue
            raise AskError(f"Claude API error {e.code}: {msg or e.reason}") from e
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2)
                continue
            raise AskError(f"Couldn't reach the Claude API: {e.reason}") from e
    raise AskError("Claude API unavailable")


def _json_answer(data: dict) -> dict:
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AskError("Claude declined this request. Try wording it differently.")
    if stop == "max_tokens":
        raise AskError("Claude's answer was cut off. Please try again.")
    block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    try:
        return json.loads(block["text"]) if block else {}
    except ValueError as e:
        raise AskError("Claude's answer wasn't in the expected format. Please try again.") from e


def _img(jpeg_b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64}}


def _validate_settings(items: list, settings: list[dict]) -> tuple[list[dict], list[str]]:
    by_name = {s["name"]: s for s in settings if s.get("writable") and s.get("choices")}
    changes, dropped = [], []
    for item in items or []:
        s = by_name.get(item.get("name"))
        choice = next((c for c in (s or {}).get("choices", []) if c["value"] == item.get("value")), None)
        if s is None or choice is None:
            dropped.append(f"{item.get('name')}={item.get('value')}")
            continue
        changes.append({"name": s["name"], "value": choice["value"], "label": choice["label"],
                        "from_label": s["label"], "same": choice["value"] == s["value"]})
    return changes, dropped


# ---------------------------------------------------------------- Lighting Lab
LIGHT_ROLES = ["key", "fill", "back", "rim", "hair", "kicker", "background", "practical", "bounce", "negative"]

LIGHTING_SYSTEM = (
    "You are a senior gaffer and director of photography. From reference images (and optionally "
    "the camera's current frame) work out how the reference look was lit and give a precise, "
    "buildable lighting plan plus camera settings for a Sony ZV-E10 to recreate it. Read the "
    "light like a pro: direction and height of the key from the catchlights, nose and cheek "
    "shadows; hardness from shadow edges; key:fill ratio from the shadow side; rim/back light "
    "from edge highlights; colour temperature and gels from skin and background; background "
    "separation and practicals. Use this top-down coordinate system: the subject stands at the "
    "centre facing the camera; angle_deg is measured around the subject from the camera axis, "
    "0 = from the camera position, +90 = camera right, -90 = camera left, 180 or -180 = directly "
    "behind the subject. distance_m is from the subject, height_m is the light's height above "
    "the floor (eye level is about 1.6 m). power_stops is relative to the key light (key = 0, "
    "fill e.g. -2 for a 4:1 ratio). Prefer gear a small studio or home setup has (LED panels, "
    "COB lights with softbox/umbrella, practical lamps, a window, a white bounce, black "
    "negative fill); name the modifier. Steps must be short, ordered and practical. Camera "
    "settings must use only the allowed values listed, and include only the ones to change. "
    "Keep text short and professional, no filler."
)


def _lighting_schema(setting_names: list[str]) -> dict:
    light = {"type": "object", "additionalProperties": False,
             "required": ["role", "fixture", "modifier", "angle_deg", "distance_m", "height_m",
                          "power_stops", "color_temp_k", "gel", "note"],
             "properties": {"role": {"type": "string", "enum": LIGHT_ROLES},
                            "fixture": {"type": "string"}, "modifier": {"type": "string"},
                            "angle_deg": {"type": "number"}, "distance_m": {"type": "number"},
                            "height_m": {"type": "number"}, "power_stops": {"type": "number"},
                            "color_temp_k": {"type": "integer"}, "gel": {"type": "string"},
                            "note": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["title", "look_analysis", "key_fill_ratio", "contrast", "lights", "background",
                         "practicals", "steps", "camera_settings", "camera_notes"],
            "properties": {
                "title": {"type": "string"},
                "look_analysis": {"type": "string"},
                "key_fill_ratio": {"type": "string"},
                "contrast": {"type": "string", "enum": ["low-key", "high-key", "mid-key"]},
                "lights": {"type": "array", "items": light},
                "background": {"type": "string"},
                "practicals": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
                "camera_settings": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["name", "value"],
                    "properties": {"name": {"type": "string", "enum": setting_names},
                                   "value": {"type": "integer"}}}},
                "camera_notes": {"type": "string"}}}


def lighting_plan(refs_b64: list[str], live_jpeg: bytes | None, settings: list[dict],
                  request: str, mode: str, gear: str = "") -> dict:
    """Lighting diagram data + validated camera settings to recreate a reference look."""
    key = _key()
    if not key:
        raise AskError("NO_KEY")
    writable = [s for s in settings if s.get("writable") and s.get("choices")]
    lines = [f"- {s['name']}: currently {s['value']} ({s['label']}). Allowed: " +
             ", ".join(f"{c['value']} ({c['label']})" for c in s["choices"]) for s in writable]
    content = []
    for i, r in enumerate(refs_b64[:4]):
        content += [{"type": "text", "text": f"Reference image {i + 1}:"}, _img(r)]
    if live_jpeg:
        content += [{"type": "text", "text": "The camera's current live view (the real set):"},
                    _img(base64.standard_b64encode(live_jpeg).decode())]
    gear_text = (("\n\nThe person's lighting gear (use ONLY this gear; if the look truly needs something they "
                  "don't have, say so in a step and give the closest result with what they have):\n" + gear.strip())
                 if gear and gear.strip() else "")
    content.append({"type": "text", "text": (
        f"Section: {mode}.{gear_text}\nCamera settings and allowed values:\n" + "\n".join(lines) +
        "\n\nRequest: " + (request.strip() or ("Recreate the look of the reference." if refs_b64
                                                else "Design the most professional lighting for this scene.")))})
    body = {"model": MODEL, "max_tokens": 16000, "system": cam(LIGHTING_SYSTEM),
            "output_config": {"effort": "medium", "format": {"type": "json_schema",
                                                             "schema": _lighting_schema([s["name"] for s in writable])}},
            "fallbacks": "default", "messages": [{"role": "user", "content": content}]}
    plan = _json_answer(call(body, key))
    for L in plan.get("lights", []):
        L["angle_deg"] = ((float(L.get("angle_deg", 0)) + 180) % 360) - 180
        L["distance_m"] = min(max(float(L.get("distance_m", 2)), 0.2), 20)
        L["height_m"] = min(max(float(L.get("height_m", 1.8)), 0), 8)
    plan["camera"], plan["dropped"] = _validate_settings(plan.pop("camera_settings", []), writable)
    return plan


COMPARE_SYSTEM = (
    "You are a director of photography checking the set against reference images. Compare the "
    "camera's live view to the reference look: light direction, key:fill ratio, hardness, rim and "
    "background separation, colour temperature, exposure and depth of field. Give a match score "
    "(0-100) and at most six concrete fixes in order of impact, each saying exactly what to move "
    "or change (e.g. 'Move the key 30 degrees further camera-left and 30 cm higher'). Short, "
    "professional, no filler."
)


def compare_to_reference(refs_b64: list[str], live_jpeg: bytes, plan_title: str = "") -> dict:
    key = _key()
    if not key:
        raise AskError("NO_KEY")
    content = []
    for i, r in enumerate(refs_b64[:4]):
        content += [{"type": "text", "text": f"Reference {i + 1}:"}, _img(r)]
    content += [{"type": "text", "text": "Live view now:"}, _img(base64.standard_b64encode(live_jpeg).decode()),
                {"type": "text", "text": f"Target look: {plan_title or 'match the reference'}."}]
    schema = {"type": "object", "additionalProperties": False, "required": ["score", "verdict", "fixes"],
              "properties": {"score": {"type": "integer"}, "verdict": {"type": "string"},
                             "fixes": {"type": "array", "items": {
                                 "type": "object", "additionalProperties": False,
                                 "required": ["aspect", "observation", "fix"],
                                 "properties": {"aspect": {"type": "string"}, "observation": {"type": "string"},
                                                "fix": {"type": "string"}}}}}}
    body = {"model": MODEL, "max_tokens": 8000, "system": cam(COMPARE_SYSTEM),
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            "fallbacks": "default", "messages": [{"role": "user", "content": content}]}
    out = _json_answer(call(body, key))
    out["score"] = min(max(int(out.get("score", 0)), 0), 100)
    return out
