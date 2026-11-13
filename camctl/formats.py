"""Formats: what each mode records, chosen by the owner from what the connected camera offers.

- Nothing here is fixed to one camera: every choice comes from the camera's own allowed-value lists (the
  values it reports as settable right now), named where the camera profile knows the name.
- "Studio's choice" is today's behaviour and stays the default: photos RAW + JPEG (the profile's RAW + JPEG
  value), the photo picture as the camera has it (log set back to normal unless "Photo preview like video"),
  video in the profile's log picture profile, 4K when offered, at the best bitrate for the frame rate.
- A choice is stored per mode (photo / video) in %APPDATA%\\camctl\\formats.json, so the Photo/Video switch keeps
  each mode's own format. The camera only reports a mode's lists while it is in that mode, so the last lists
  seen per mode are kept too (to show the other mode's rows).
Nothing here talks to the camera: webui / capture write the values, this module only decides them.
"""
from __future__ import annotations

import json
import os
import re
import threading

from . import camprofile

# Sony property codes (the driver's numbering)
SP_STILL_FORMAT, SP_ASPECT, SP_PICTURE_PROFILE, SP_MOVIE_FORMAT, SP_MOVIE_SETTING = 0xD253, 0xD211, 0xD23F, 0xD241, 0xD242
SP_JPEG_QUALITY, SP_IMAGE_SIZE = 0xD25F, 0xD203
CODES = {"still_format": SP_STILL_FORMAT, "jpeg_quality": SP_JPEG_QUALITY, "image_size": SP_IMAGE_SIZE, "aspect": SP_ASPECT,
         "picture_profile": SP_PICTURE_PROFILE, "movie_format": SP_MOVIE_FORMAT, "movie_setting": SP_MOVIE_SETTING}
# in the order a person thinks: what file, how good, how big, what shape, what look
ROWS = {"photo": ["still_format", "jpeg_quality", "image_size", "aspect", "picture_profile"],
        "video": ["movie_format", "movie_setting", "picture_profile"]}
TITLES = {("photo", "still_format"): "Photo file", ("photo", "jpeg_quality"): "JPEG quality",
          ("photo", "image_size"): "Image size", ("photo", "aspect"): "Photo aspect ratio",
          ("photo", "picture_profile"): "Photo picture profile", ("video", "picture_profile"): "Video picture profile",
          ("video", "movie_format"): "Video file", ("video", "movie_setting"): "Frame rate and quality"}

STILL = {1: "RAW", 2: "RAW + JPEG", 3: "JPEG"}
JPEG_QUALITY = {1: "Standard", 3: "Fine", 4: "Extra fine"}
IMAGE_SIZE = {1: "L (largest)", 2: "M", 3: "S"}
ASPECT = {1: "3:2", 2: "16:9", 3: "4:3", 4: "1:1"}          # Sony numbering (2 = 16:9 confirmed: 6000x3376 photos)
ASPECT_RATIO = {1: 3 / 2, 2: 16 / 9, 3: 4 / 3, 4: 1.0}
MOVIE_FORMAT = {8: "4K video (XAVC S)", 9: "HD video (XAVC S)"}
LOG_CURVES = {"slog3": (0.41, 0.0887), "slog2": (0.326, 0.085), "log": (0.41, 0.0887)}   # mid grey, one stop (0..1)

_lock = threading.Lock()


def _file():
    return camprofile.BASE / "formats.json"


def _read() -> dict:
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def choices_made() -> dict:
    """{"photo": {key: value}, "video": {...}}: only what the owner picked (absent = Studio's choice)."""
    d = _read().get("chosen") or {}
    return {m: {k: v for k, v in (d.get(m) or {}).items() if k in ROWS[m] and isinstance(v, int)} for m in ROWS}


def chosen(mode: str, key: str):
    return choices_made()[mode].get(key)


def set_choice(mode: str, key: str, value) -> None:
    """Store his pick (None = back to Studio's choice)."""
    if mode not in ROWS or key not in ROWS[mode]:
        raise KeyError(key)
    with _lock:
        d = _read()
        c = d.setdefault("chosen", {}).setdefault(mode, {})
        if value is None:
            c.pop(key, None)
        else:
            c[key] = int(value)
        if mode == "video" and key == "movie_format":
            c.pop("movie_setting", None)          # the rate list depends on the file: pick again from the new list
        _write(d)


def set_mode_choices(mode: str, values: dict) -> None:
    """A saved setup's choices for one mode, all at once (keys not in it go back to Studio's choice)."""
    with _lock:
        d = _read()
        d.setdefault("chosen", {})[mode] = {k: int(v) for k, v in (values or {}).items()
                                            if k in ROWS[mode] and isinstance(v, int)}
        _write(d)


# ---------------------------------------------------------------- what the camera offers
def _offered(p) -> list[int]:
    if p is None:
        return []
    if p.choices:
        return [c for c in p.choices if isinstance(c, int)]
    return []


def remember(mode: str, props) -> None:
    """Keep the lists the camera reports in this mode (it hides the other mode's lists)."""
    seen = {}
    for key in ROWS[mode]:
        p = props.get(CODES[key])
        if p is None:
            continue
        seen[key] = {"value": p.value, "choices": _offered(p), "settable": bool(p.writable and p.enabled and _offered(p)),
                     "locked": bool(p.writable and not p.enabled)}
    try:
        with _lock:
            d = _read()
            if (d.get("seen") or {}).get(mode) != seen:
                d.setdefault("seen", {})[mode] = seen
                _write(d)
    except OSError:
        pass


def seen(mode: str) -> dict:
    return (_read().get("seen") or {}).get(mode) or {}


# ---------------------------------------------------------------- names
def pp_name(profile: dict, v) -> str:
    """'Off (standard)', a named slot ('PP8 · S-Log3 (Cine)') where the profile knows it, else 'Profile 3'."""
    if v is None:
        return "—"
    if v == 0:
        return "Off (standard)"
    names = profile.get("pp_names") or {}
    n = names.get(v) or names.get(str(v))
    if n:
        return f"{n} · picture profile {v}"
    if camprofile.is_log(profile, v):
        return f"Profile {v} (log)"
    return f"Profile {v}"


def pp_short(profile: dict, v) -> str:
    """The short name for sentences: "S-Log3", "HLG", "Standard picture", "Profile 3"."""
    if v in (None, 0):
        return "Standard picture"
    return pp_name(profile, v).split(" · ")[0].split(" (")[0]


def pp_kind(profile: dict, v) -> str:
    """'log', 'hlg' or 'standard' (exposure and monitoring follow this; unknown slots count as standard)."""
    if v is None or v == 0:
        return "standard"
    if camprofile.is_log(profile, v):
        return "log"
    if v in (profile.get("hlg_pps") or []):
        return "hlg"
    return "standard"


def movie_setting_name(raw: str) -> str:
    """'4K 25p 100M' -> '4K · 25p · 100 Mbps'; '50p 50M' -> 'HD · 50p · 50 Mbps'."""
    m = re.match(r"(4K\s+)?(\d+p)\s+(\d+)M", raw or "")
    if not m:
        return raw or "—"
    return f"{'4K' if m.group(1) else 'HD'} · {m.group(2)} · {m.group(3)} Mbps"


def value_name(key: str, v, profile: dict, label_fn=None) -> str:
    if v is None:
        return "—"
    if key == "still_format":
        return STILL.get(v, f"Type {v}")
    if key == "jpeg_quality":
        return JPEG_QUALITY.get(v, f"Quality {v}")
    if key == "image_size":
        return IMAGE_SIZE.get(v, f"Size {v}")
    if key == "aspect":
        return ASPECT.get(v, f"Ratio {v}")
    if key == "picture_profile":
        return pp_name(profile, v)
    if key == "movie_format":
        return MOVIE_FORMAT.get(v, f"Format {v}")
    if key == "movie_setting":
        return movie_setting_name(label_fn("movie_setting", v) if label_fn else str(v))
    return str(v)


def fps_of(label: str) -> int | None:
    m = re.search(r"(\d+)p", label or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- Studio's choice and his choice
def _log_pp(profile: dict):
    return profile.get("log_pp") if (profile.get("builtin") or profile.get("log_confirmed")) else None


def best_movie_setting(choices: list[int], label_fn, fps: int | None) -> int | None:
    """Highest bitrate at this frame rate, 4K first (today's rule)."""
    labs = {v: label_fn("movie_setting", v) for v in choices}
    good = [v for v in choices if re.search(r"\d+p", labs[v] or "")]

    def key(v):
        lab = labs[v]
        rate = int(re.search(r"(\d+)M", lab).group(1)) if re.search(r"(\d+)M", lab) else 0
        return ("4K" in lab, fps is None or fps_of(lab) == fps, rate)
    return max(good, key=key) if good else None


def studio_value(mode: str, key: str, props, profile: dict, label_fn, photo_log: bool = False):
    """What Studio's choice sets for this row now (None = Studio leaves it as the camera has it)."""
    offered = _offered(props.get(CODES[key]))
    p = props.get(CODES[key])
    if mode == "photo" and key == "still_format":
        v = profile.get("still_raw_jpeg", 2)
        return v if v in offered else None
    if mode == "photo" and key == "jpeg_quality":       # the best quality the camera offers
        known = [v for v in offered if v in JPEG_QUALITY]
        return max(known, key=lambda v: list(JPEG_QUALITY).index(v)) if known else None
    if mode == "photo" and key == "image_size":
        return 1 if 1 in offered else None                # L: the largest
    if mode == "photo" and key == "aspect":
        return None
    if mode == "photo" and key == "picture_profile":
        lp = _log_pp(profile)
        if photo_log and lp is not None and lp in offered:
            return lp
        if p is not None and camprofile.is_log(profile, p.value) and 0 in offered:
            return 0                                # log left on from video: back to the normal picture
        return None
    if mode == "video" and key == "picture_profile":
        lp = _log_pp(profile)
        return lp if lp is not None and lp in offered else None
    if mode == "video" and key == "movie_format":
        four_k = [v for v in offered if "4K" in MOVIE_FORMAT.get(v, "")]
        return four_k[0] if four_k else None
    if mode == "video" and key == "movie_setting":
        if not offered:
            return None
        cur = fps_of(label_fn("movie_setting", p.value)) if p is not None else None
        return best_movie_setting(offered, label_fn, cur or 25)
    return None


def wanted(mode: str, key: str, props, profile: dict, label_fn, photo_log: bool = False):
    """(value to set or None to leave, note): his pick when the camera offers it, else Studio's choice."""
    mine = chosen(mode, key)
    offered = _offered(props.get(CODES[key]))
    if mine is not None:
        if mine in offered:
            return mine, ""
        if key == "movie_setting" and offered:      # same frame rate in the new list, else Studio's best
            fps = fps_of(label_fn("movie_setting", mine))
            alt = best_movie_setting([v for v in offered if fps_of(label_fn("movie_setting", v)) == fps], label_fn, fps)
            if alt is not None:
                return alt, ""
        note = (f"{value_name(key, mine, profile, label_fn)} is not offered by the camera right now, "
                f"so Studio kept {TITLES[(mode, key)].lower()} as it is.")
        return studio_value(mode, key, props, profile, label_fn, photo_log), note
    return studio_value(mode, key, props, profile, label_fn, photo_log), ""


def default_name(mode: str, key: str, profile: dict) -> str:
    """Studio's choice in words, for the reset button."""
    if mode == "photo" and key == "still_format":
        return STILL.get(profile.get("still_raw_jpeg", 2), "RAW + JPEG")
    if mode == "photo" and key == "jpeg_quality":
        return "best quality"
    if mode == "photo" and key == "image_size":
        return "largest (L)"
    if mode == "photo" and key == "aspect":
        return "as the camera has it"
    if mode == "photo" and key == "picture_profile":
        return "camera standard"
    if mode == "video" and key == "picture_profile":
        lp = _log_pp(profile)
        return pp_name(profile, lp) if lp is not None else "as the camera has it"
    if mode == "video" and key == "movie_format":
        return "4K when offered"
    return "best quality at this frame rate"


# ---------------------------------------------------------------- the rows for the page
def rows(mode: str, props, profile: dict, label_fn, active: bool, photo_log: bool = False) -> list[dict]:
    """The Format rows of one mode. Active mode: the camera's lists now; the other mode: the last lists seen."""
    out = []
    old = seen(mode)
    for key in ROWS[mode]:
        if active:
            p = props.get(CODES[key])
            if p is None:
                continue
            cur, offered, settable = p.value, _offered(p), bool(p.writable and p.enabled and _offered(p))
            locked = bool(p.writable and not p.enabled)
        else:
            s = old.get(key)
            if not s:
                continue
            cur, offered, settable = s.get("value"), list(s.get("choices") or []), bool(s.get("settable"))
            locked = bool(s.get("locked"))
        mine = chosen(mode, key)
        row = {"mode": mode, "key": key, "title": TITLES[(mode, key)], "active": active,
               "value": cur, "value_name": value_name(key, cur, profile, label_fn),
               "choices": [{"value": v, "label": value_name(key, v, profile, label_fn)} for v in offered],
               "settable": settable and len(offered) > 1, "chosen": mine,
               "chosen_name": value_name(key, mine, profile, label_fn) if mine is not None else None,
               "studio": default_name(mode, key, profile), "note": ""}
        if key == "picture_profile":
            row["kind"] = pp_kind(profile, mine if mine is not None else cur)
        if not row["settable"]:
            row["note"] = ("This is the only one the camera offers here." if settable and len(offered) == 1
                           else "The camera keeps this fixed with the current file type or mode." if locked
                           else "Set this in the camera menu.")
        if key == "picture_profile" and mode == "photo" and row["kind"] == "log":
            row["note"] = "Photos look flat like your log video; the RAW is not affected."
        out.append(row)
    return out


def summary(mode: str, props, profile: dict, label_fn) -> dict:
    """The format in use, in words (status sentence, HUD, report), and the recorded picture ratio."""
    g = lambda code: (props.get(code).value if props.get(code) is not None else None)
    pp = g(SP_PICTURE_PROFILE)
    kind = pp_kind(profile, pp)
    if mode == "video":
        ms = g(SP_MOVIE_SETTING)
        ppn = pp_short(profile, pp)                                     # "S-Log3", "HLG", "Standard picture"
        name = " · ".join([ppn] + ([movie_setting_name(label_fn("movie_setting", ms))] if ms is not None else []))
        return {"mode": mode, "name": name, "ratio": 16 / 9, "ratio_name": "16:9", "pp_kind": kind, "jpeg_only": False}
    sf, asp = g(SP_STILL_FORMAT), g(SP_ASPECT)
    name = STILL.get(sf, "") if sf is not None else ""
    if asp in ASPECT:
        name = f"{name} · {ASPECT[asp]}" if name else ASPECT[asp]
    return {"mode": mode, "name": name, "ratio": ASPECT_RATIO.get(asp), "ratio_name": ASPECT.get(asp),
            "pp_kind": kind, "jpeg_only": sf == 3}
