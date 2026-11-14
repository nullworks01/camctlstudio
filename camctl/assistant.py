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
# The ONE place the models are named. Studio's choice is Claude Opus 5.5; he can choose Claude Fable 5.2 in
# All settings › Studio › Claude › Model (prefs "claude_model"). shape() builds the request for whichever is chosen.
OPUS, FABLE = "claude-opus-5-5", "claude-fable-5-2"
MODELS = {OPUS: "Claude Opus 5.5", FABLE: "Claude Fable 5.2"}
MODEL_LINES = {OPUS: "Fast and precise. Studio's choice.",
               FABLE: "Most capable. Answers take longer and cost about twice as much."}
MODEL = OPUS                        # Studio's choice (the default)
MODEL_NAME = MODELS[MODEL]
# Studio's own safety net, only when Fable is chosen: if Fable is refused with 400 / 403 / 404 (not available for
# this key, or a request shape it does not take), ONE retry with Opus 5.5 and its shape. One press = one answer.
FALLBACK_MODEL = OPUS
FALLBACK_NAME = MODELS[OPUS]
PRICES = {OPUS: (4.0, 20.0), FABLE: (8.0, 40.0)}               # list price, $ per million tokens (in, out)


def _prefs_file():
    from . import setups
    return setups.BASE / "prefs.json"


def chosen_model() -> str:
    """His choice from the prefs file, else Studio's choice (Opus 5.5)."""
    try:
        m = json.loads(_prefs_file().read_text(encoding="utf-8")).get("claude_model")
    except (OSError, ValueError, AttributeError):
        m = None
    return m if m in MODELS else MODEL


def choose_model(model: str | None) -> str:
    """Store his choice (None = back to Studio's choice) in the normal prefs file, keeping the other prefs."""
    f = _prefs_file()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if model in MODELS and model != MODEL:
        d["claude_model"] = model
    else:
        d.pop("claude_model", None)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d), encoding="utf-8")
    return chosen_model()
TIMEOUT_S = 120                     # Fable thinks longer: one press may take a minute or two
MIN_MAX_TOKENS = 16000              # thinking counts against max_tokens: a few hundred would cut the answer off
# effort per button (the feature tag of the call): quick setup decisions low, the thinking buttons high
EFFORT = {"operator decision": "low", "shot set-up": "low", "white balance": "low", "teleprompter": "low",
          "lighting lab": "high", "take review": "high", "claude: direct": "high", "claude: best take": "high",
          "key test": "low", "sound check": "low", "shoot for a mood": "high"}

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
    # Tiny on purpose: low effort, a one-word prompt. Only the HTTP answer matters (a reply cut short by the small
    # max_tokens still proves the key works), so this costs a fraction of a cent.
    def once(model):
        body = {"model": model, "max_tokens": 64, "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": "Reply with OK."}]}
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-api-key": key,
                                              "anthropic-version": "2026-10-21"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        usage.record(data.get("usage") or {}, model)
    m = chosen_model()
    try:
        try:
            once(m)
            return "ok", f"Claude is connected ({MODELS[m]})."
        except urllib.error.HTTPError as e0:            # any 200 above counts: the text is never looked at
            if m != FABLE or e0.code not in (400, 403, 404):
                raise
            _oplog(f"Key test: {MODELS[FABLE]} answered {e0.code}: trying {FALLBACK_NAME}")
            once(FALLBACK_MODEL)
            return "ok", (f"The key works, but {MODELS[FABLE]} isn't available for it: Studio will use "
                          f"{FALLBACK_NAME}.")
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
            return "busy", "Studio couldn't check the key right now. Try again in a minute."
        return "error", f"Claude answered with an error ({e.code}). Try again in a minute."
    except (urllib.error.URLError, TimeoutError, OSError):
        return "offline", "Studio couldn't reach Claude to check the key. Check the internet connection."


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


FORBIDDEN = ("temperature", "top_p", "top_k")

# ---------------------------------------------------------------- the situation: ONE place for every camera button
# webui sets SITUATION to a function returning {mode, format, pp_kind, fps, shutter_rule, aspect, shot, locked,
# offered, last_take}; shape() adds the role for the mode and this block to the system prompt of every camera button.
SITUATION = None
CAMERA_FEATURES = {"claude: direct", "take review", "claude: best take", "lighting lab", "operator decision",
                   "shot set-up", "shoot for a mood"}
ROLE = {
    "photo": ("Think as a photographer: the moment, composition, depth of field, the light on the subject; the "
              "shutter is free above the safe handheld speed; when choosing between photos, pick the best moment."),
    "video": ("Think as a director of photography: the shutter is tied to the frame rate (never change it), keep "
              "continuity between takes (the same exposure and white balance as the last take unless he asks), "
              "think of movement and sound, and review the take as a whole."),
}
NO_LOOKS = "Never advise colour looks, LUTs, grading or editing; the picture is made with light, exposure and framing."
# the active tool changes the priorities: one table, not scattered ifs
TOOL_RULES = {
    "focusstack": ("Focus stack: a tripod and a still object; the lens's sharpest aperture (about f/5.6 to f/8), not the "
                   "widest; exposure and white balance identical across all frames; manual focus steps; never suggest "
                   "changes between frames."),
    "long": ("Long exposure: a low ISO and a slow shutter are the aim, so never advise a faster shutter or a higher ISO; "
             "think about ambient light leaking in, the tripod, and where the drawing with light sits in the frame."),
    "double": ("Double exposure: think about how the two pictures fit: a dark area in the first for the second to show "
               "through, and where the subject of the second should sit; expose each photo with that in mind."),
    "pull": ("Focus pull: depth of field at the chosen aperture, the distance between point A and point B, and a speed "
             "that suits the move; in video the shutter rule stays."),
    "reel": "Day reel: the 3 seconds before each photo become a small silent clip; advice about that moment only, short.",
    "sound": "Sound check: advice about the microphone and the room only, short.",
    "teleprompter": "Teleprompter: advice about reading naturally to the lens and the eye line only, short.",
    "shotlist": "Shot list: advice that fits the next shot on the list only, short.",
}


def situation_text(s: dict | None, feature: str = "") -> str:
    """The role for the mode, the format rule and the facts, as one block (also for tests)."""
    if not s:
        return ""
    mode = "video" if s.get("mode") == "video" else "photo"
    lines = [f"Situation: {mode} mode.", ROLE[mode]]
    if s.get("format"):
        lines.append(f"Format in use: {s['format']}.")
    if s.get("pp_kind") == "log":
        lines.append("A log profile is on: expose for grading later (protect highlights, about +1 to +1.5 stops over "
                     "mid grey on skin) and say that the flat picture is expected.")
    else:
        lines.append("A normal picture profile is on: expose for the finished picture as it will be seen.")
    if mode == "video" and s.get("shutter_rule"):
        lines.append(f"Shutter rule: {s['shutter_rule']}.")
    for k, t in (("aspect", "Aspect ratio"), ("shot", "Shot type"), ("locked", "Locked by him"),
                 ("offered", "The camera offers"), ("last_take", "Last take")):
        if s.get(k):
            lines.append(f"{t}: {s[k]}.")
    for t in s.get("tools") or []:
        if t.get("key") in TOOL_RULES:
            lines.append(f"Tool in use: {t.get('detail') or t['key']}. {TOOL_RULES[t['key']]}")
    if s.get("look_preview"):
        lines.append(f"He is previewing the look '{s['look_preview']}' on his screen only; the picture you get is the "
                     "untouched camera frame. Do not comment on the look.")
    if feature == "shoot for a mood":
        lines.append("This is the creative mood experiment: you may leave the 'correct' exposure on purpose when the "
                     "mood needs it, and say so.")
    lines.append(NO_LOOKS)
    return "\n".join(lines)


def shape(body: dict, model: str | None = None) -> dict:
    """The request for the chosen model. Opus 5.5 (Studio's choice): the shape proven before, as the caller wrote it,
    plus the server-side fallback on a refusal. Fable 5.2: no thinking setting (always on), no sampling parameters,
    no forced tool choice (auto + strict tools, the prompt names the tool), max_tokens large enough for thinking plus
    the answer, effort per button, the server-side fallback on a refusal."""
    model = model or chosen_model()
    feat = FEATURE.get()
    if feat in CAMERA_FEATURES and SITUATION is not None:      # the same situation for every camera button
        try:
            extra = situation_text(SITUATION(), feat)
        except Exception:  # noqa: BLE001 - a missing fact never blocks the question
            extra = ""
        if extra:
            sys_ = body.get("system")
            sys_ = (sys_ + "\n\n" if isinstance(sys_, str) and sys_ else "") + extra
            body = {**body, "system": sys_}
    if model != FABLE:
        b = {k: v for k, v in body.items() if k not in FORBIDDEN}
        b["model"] = model
        b["fallbacks"] = "default"
        return b
    b = {k: v for k, v in body.items() if k not in FORBIDDEN}
    b["model"] = FABLE
    th = b.pop("thinking", None)
    if isinstance(th, dict) and th.get("type") == "adaptive":
        b["thinking"] = {"type": "adaptive"}
    tc = b.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("any", "tool"):
        b["tool_choice"] = {"type": "auto"}                  # forced use is a 400 here: auto + strict instead
        if b.get("tools"):
            b["tools"] = [{**t, "strict": True} if "input_schema" in t else t for t in b["tools"]]
    b["max_tokens"] = max(int(b.get("max_tokens") or 0), MIN_MAX_TOKENS)
    oc = dict(b.get("output_config") or {})
    oc["effort"] = EFFORT.get(FEATURE.get(), oc.get("effort") or "high")
    b["output_config"] = oc
    msgs = list(b.get("messages") or [])
    while msgs and msgs[-1].get("role") == "assistant":       # no prefill on this model
        msgs.pop()
    b["messages"] = msgs
    b["fallbacks"] = "default"
    return b


def _send(body: dict, key: str) -> dict:
    if not key:                                  # never a request without a key: it could only fail as "wrong key"
        raise AskError("NO_KEY")
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2026-10-21",
                 "anthropic-beta": "server-side-fallback-2026-07-01"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except TimeoutError as e:                    # one press = one answer: no automatic second try
        raise AskError(f"Claude took longer than {TIMEOUT_S // 60} minutes to answer. Nothing was changed; "
                       "press again to try once more.") from e
    usage.record(data.get("usage") or {}, data.get("model") or body.get("model"))
    return data


FALLBACKS: list = []          # (time, feature) of answers that came from FALLBACK_MODEL (the page says so)


def _oplog(text: str) -> None:
    """The reason goes into operator.log (never the key, never the prompt)."""
    try:
        from . import autofix
        autofix._log_event(text, "claude")
    except Exception:  # noqa: BLE001
        pass


_NOT_FOR_THIS_KEY = {"until": 0.0}    # after a 404 (model not available for this key): straight to the fallback


def _post(body: dict, key: str) -> dict:
    m = chosen_model()
    shaped = shape(body, m)
    if m != FABLE:                                         # Opus 5.5: no retry on another model
        return _send(shaped, key)
    try:
        if time.time() < _NOT_FOR_THIS_KEY["until"]:
            data = _send(shape(body, FALLBACK_MODEL), key)
            data["_answered_by"] = FALLBACK_NAME
            FALLBACKS.append((time.time(), FEATURE.get()))
            return data
        return _send(shaped, key)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 403, 404) or time.time() < _NOT_FOR_THIS_KEY["until"]:
            raise
        if e.code == 404:
            _NOT_FOR_THIS_KEY["until"] = time.time() + 3600
        try:
            why = str(json.loads(e.read()).get("error", {}).get("message", ""))[:160]
        except (ValueError, OSError):
            why = ""
        why = why.replace(key, "") if key else why
        _oplog(f"{MODELS[FABLE]} answered {e.code} ({why or 'no reason given'}): asking {FALLBACK_NAME} once instead")
        prev = shape(body, FALLBACK_MODEL)                    # Opus 5.5 in its own proven shape
        data = _send(prev, key)
        data["_answered_by"] = FALLBACK_NAME
        FALLBACKS.append((time.time(), FEATURE.get()))
        del FALLBACKS[:-20]
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

    def record(self, u: dict, model: str | None = None) -> None:
        set_problem(None)                       # a call that worked: no problem to show any more
        mark_ok()
        inp = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
            int(u.get("cache_creation_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        pin, pout = PRICES.get(model or MODEL, PRICES[MODEL])
        cost = inp / 1e6 * pin + out / 1e6 * pout          # list price of the model that answered
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
                bucket["cost"] = round(bucket.get("cost", 0.0) + cost, 6)
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(d), encoding="utf-8")

    def today(self) -> dict:
        d = self.load()
        feats = d["days"].get(time.strftime("%Y-%m-%d"), {})
        tot = {"calls": sum(f.get("calls", 0) for f in feats.values()),
               "input": sum(f.get("input", 0) for f in feats.values()),
               "output": sum(f.get("output", 0) for f in feats.values()),
               "cost": round(sum(f.get("cost", 0.0) for f in feats.values()), 4)}
        return {"total": tot, "features": feats, "shoots": d.get("shoots", {}),
                "model": MODELS[chosen_model()], "model_id": chosen_model(), "fallback_model": FALLBACK_NAME,
                "models": [{"id": k, "name": v, "line": MODEL_LINES[k]} for k, v in MODELS.items()], "studio_model": MODEL,
                "price": {"input": PRICES[chosen_model()][0], "output": PRICES[chosen_model()][1]}}


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


MOOD_SYSTEM = (
    "You are a director of photography helping one person reach a mood with a Sony ZV-E10 and the light they have. "
    "You see the camera's current, untouched picture and the ranges the camera really offers. Propose camera settings "
    "(aperture, shutter, ISO, white balance in kelvin) inside those ranges, a composition guide, where to put the light "
    "in two short sentences, at most three practical changes in the room, and honestly what this camera cannot do "
    "here. Never suggest a colour look, a LUT, a filter preset, grading or any editing afterwards: the mood must come "
    "from light, exposure, white balance, framing and the room. Plain, short, friendly words.")


def mood(live_view_jpeg: bytes, words: str, ranges: dict) -> dict:
    """Shoot for a mood: ONE call. ranges: {aperture:[min,max], shutter_s:[min,max], iso:[min,max], kelvin:[min,max],
    mode, fixed_shutter_s or None}. Returns the strict JSON answer (the caller clamps it to the camera's values)."""
    key = _key()
    if not key:
        raise AskError("NO_KEY")
    schema = {"type": "object", "additionalProperties": False,
              "required": ["aperture_f", "shutter_s", "iso", "kelvin", "guide", "light", "room", "cannot"],
              "properties": {"aperture_f": {"type": "number"}, "shutter_s": {"type": "number"},
                             "iso": {"type": "integer"}, "kelvin": {"type": "integer"},
                             "guide": {"type": "string", "enum": ["thirds", "symmetry", "centre", "space left", "space right"]},
                             "light": {"type": "string"}, "room": {"type": "array", "items": {"type": "string"}},
                             "cannot": {"type": "string"}}}
    fixed = ranges.get("fixed_shutter_s")
    text = (f"The mood he wants: {words.strip()[:300]}\nMode: {ranges.get('mode')}.\n"
            f"Aperture f/{ranges['aperture'][0]:g} to f/{ranges['aperture'][1]:g}; ISO {ranges['iso'][0]} to {ranges['iso'][1]}; "
            f"white balance {ranges['kelvin'][0]} to {ranges['kelvin'][1]} K; "
            + (f"the shutter stays {fixed:g} s (video: it follows the frame rate)." if fixed else
               f"shutter {ranges['shutter_s'][0]:g} s to {ranges['shutter_s'][1]:g} s.")
            + " Answer in the JSON format.")
    body = {"model": MODEL, "max_tokens": 4000, "system": cam(MOOD_SYSTEM),
            "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            "fallbacks": "default",
            "messages": [{"role": "user", "content": [
                _img(base64.standard_b64encode(live_view_jpeg).decode()), {"type": "text", "text": text}]}]}
    with feature("shoot for a mood"):
        out = _json_answer(call(body, key))
    for k in ("aperture_f", "shutter_s", "iso", "kelvin"):
        if not isinstance(out.get(k), (int, float)):
            raise AskError("Claude's answer wasn't in the expected format. Please try again.")
    out["room"] = [str(x) for x in (out.get("room") or [])][:3]
    return out


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
