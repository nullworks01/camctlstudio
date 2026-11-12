"""Smart presets for camctl Studio (data in presets.json).

A preset states an intent (aperture preference, slowest acceptable shutter,
ISO comfort limit, focus, white balance) and a target brightness. A "look"
(cinematic style) adds a Kelvin white balance, a brightness bias and a colour
grade. Applying them:

1. sets the base values (mode, focus, white balance),
2. measures the scene from the live view (the ZV-E10's live view follows the
   exposure settings, so its brightness shows how far off the exposure is),
3. solves aperture / shutter / ISO for that light within the preset's rules,
   snapping to values the camera actually allows,
4. applies, re-measures and corrects once more (closed loop).
No photo is taken and no recording is started.
"""
from __future__ import annotations

import io
import dataclasses
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .drivers.sony import PROPS

DATA = json.loads((Path(__file__).with_name("presets.json")).read_text(encoding="utf-8"))

MODE_M = 1
MODE_MOVIE_M = 0x78053  # 491603, "Movie M" on Sony bodies
MODE_SQ_M = 0x9805C     # 622684, "S&Q M" (slow & quick motion)
PROP_COLOR_TEMP = 0xD20F
PROP_PICTURE_PROFILE = 0xD23F   # 0 = off, 1..10 = PP1..PP10 (per shooting mode)
PROP_WB_SHIFT_AB = 0xD21C       # 164 (blue) .. 192 .. 220 (amber), step 2
PROP_WB_SHIFT_GM = 0xD210       # 164 (magenta) .. 192 .. 220 (green), step 1
PP_NAMES = {0: "Off", 1: "PP1 Movie", 2: "PP2 Still", 3: "PP3 ITU709", 4: "PP4 ITU709 800%",
            5: "PP5 Cine1", 6: "PP6 Cine2", 7: "PP7 S-Log2", 8: "PP8 S-Log3.Cine",
            9: "PP9 S-Log3", 10: "PP10 HLG2"}
FOCUS = {"MF": 1, "AF-S": 2, "AF-C": 32772, "AF-A": 32773, "DMF": 32774}
WB = {"auto": 2, "daylight": 4, "shade": 32785, "cloudy": 32784, "tungsten": 6, "kelvin": 32786}


@dataclass
class Preset:
    key: str
    name: str
    section: str
    blurb: str = ""
    icon: str = "●"
    aperture: str = "wide"
    shutter_pref: float = 1 / 125
    shutter_min: float = 1 / 60
    shutter_fixed: float | None = None
    iso_soft_max: int = 3200
    iso_max: int = 12800
    target: float = 118
    focus: str | None = None
    wb: str | None = None
    kelvin: int | None = None
    ev_bias: float = 0.0
    movie: bool = False
    mode: int | None = None            # explicit exposure mode (e.g. S&Q for slow motion)
    picture_profile: int | None = None
    log_over: float | None = None      # S-Log3: expose this many stops over mid-grey (ETTR)
    grade: dict | None = None
    tips: list[str] = field(default_factory=list)


def _preset(section: str, d: dict) -> Preset:
    known = {k: v for k, v in d.items() if k in Preset.__dataclass_fields__}
    return Preset(section=section, **known)


PRESETS = {d["key"]: _preset("photo", d) for d in DATA["photo"]}
PRESETS.update({d["key"]: _preset("video", d) for d in DATA["video"]})
LOOKS = {d["key"]: d for d in DATA["looks"]}
EFFECTS = {d["key"]: d for d in DATA["effects"]}


def catalog() -> dict:
    """Everything the Studio page needs to draw preset/look/effect cards."""
    return DATA


def brightness(jpeg: bytes) -> tuple[float, float, float]:
    """Mean brightness (0-255), % clipped highlights, % crushed shadows."""
    from PIL import Image, ImageStat
    im = Image.open(io.BytesIO(jpeg)).convert("L")
    h = im.histogram()
    n = sum(h)
    return ImageStat.Stat(im).mean[0], 100 * sum(h[250:]) / n, 100 * sum(h[:5]) / n


def _lin(level: float) -> float:
    return max((level / 255.0) ** 2.2, 1e-4)


def shutter_seconds(v: int) -> float | None:
    if not v:
        return None
    num, den = v >> 16, v & 0xFFFF
    return num / den if den else None


def _nearest(values, target, key=lambda x: x):
    return min(values, key=lambda v: abs(math.log(key(v)) - math.log(target)))


def solve(preset: Preset, props, measured: float, ev_extra: float = 0.0) -> dict[str, int]:
    """Pick raw aperture / shutter / ISO values for the measured light."""
    ap = props[PROPS["aperture"]]
    sh = props[PROPS["shutter_speed"]]
    iso = props[PROPS["iso"]]
    ap_choices = sorted(c for c in ap.choices if c)
    sh_choices = [c for c in sh.choices if shutter_seconds(c)]
    iso_choices = sorted(c for c in iso.choices if 0 < c < 0xFFFFFF)

    cur_n = ap.value / 100
    cur_t = shutter_seconds(sh.value) or 1 / 60
    cur_iso = iso.value if 0 < iso.value < 0xFFFFFF else 800

    if preset.aperture == "wide":
        n_val = ap_choices[0]
    else:
        want = 560 if preset.aperture == "mid" else float(preset.aperture.split("/")[1]) * 100
        n_val = _nearest(ap_choices, want)
    n = n_val / 100

    if preset.log_over is not None:
        # S-Log3 is (nearly) linear in stops: ~22.6 code values (of 255) per stop,
        # 18% grey at 41% (105). Meter the log picture directly.
        need = (log_target(preset.log_over + ev_extra) - measured) / SLOG3_STOP
    else:
        target = preset.target * (2 ** ((preset.ev_bias + ev_extra) / 2.2))
        target = min(max(target, 35), 200)
        need = math.log2(_lin(target) / _lin(measured))
    need = max(min(need, 6), -6)
    k = cur_t * cur_iso / cur_n ** 2 * (2 ** need) * n ** 2   # required t·ISO

    if preset.shutter_fixed:
        t = preset.shutter_fixed
    else:
        t = preset.shutter_pref
        if k / t > preset.iso_soft_max:
            # too dark: slow the shutter, but never past the preset's slowest
            # allowed speed; beyond that, ISO goes above its comfort limit.
            t = min(preset.shutter_min, k / preset.iso_soft_max)
        elif k / t < iso_choices[0]:             # too bright: speed the shutter up
            t = k / iso_choices[0]
    s = min(max(k / t, iso_choices[0]), min(preset.iso_max, iso_choices[-1]))
    return {"aperture": n_val,
            "shutter_speed": _nearest(sh_choices, t, key=shutter_seconds),
            "iso": _nearest(iso_choices, s)}


SLOG3_GREY = 0.41 * 255    # 18% grey in S-Log3 code values (0..255)
SLOG3_STOP = 0.0887 * 255  # one stop in S-Log3


def log_target(stops_over: float) -> float:
    return SLOG3_GREY + SLOG3_STOP * stops_over


def describe(level: float) -> str:
    return ("very dark" if level < 35 else "dim" if level < 80 else "balanced" if level < 150
            else "bright" if level < 200 else "very bright")


DEFAULT_MAX_ISO = {"photo": 3200, "video": 6400}   # noise guard: above this the ZV-E10 gets grainy
PROP_DRIVE = PROPS["drive_mode"]
DRIVE_SINGLE = 1


def apply_preset(cam, preset_key: str, look_key: str | None = None, label=None,
                 max_iso: int | None = None) -> dict:
    """Run a preset (optionally with a cinematic look) on a connected SonyCamera.

    max_iso is the noise guard: ISO never goes above it (default 3200 for photos,
    6400 for video). Every setting the preset decides is set explicitly (nothing is
    left over from earlier presets) and read back at the end to verify it stuck."""
    preset = PRESETS[preset_key]
    cap = int(max_iso or DEFAULT_MAX_ISO[preset.section])
    preset = dataclasses.replace(preset, iso_max=min(preset.iso_max, cap),
                                 iso_soft_max=min(preset.iso_soft_max, cap))
    intended: dict[int, tuple[str, int]] = {}
    look = LOOKS.get(look_key or "")
    label = label or (lambda name, v: str(v))
    report = {"preset": preset.name, "look": look["name"] if look else None,
              "changed": [], "skipped": [], "tips": list(preset.tips), "max_iso": cap}
    if look:
        report["lighting"] = look["lighting"]
    props = cam.refresh()

    def try_set(name, value, pretty=None):
        code = PROPS.get(name, name)
        intended[code] = (pretty or str(name), value)
        p = props.get(code)
        if p is not None and p.value == value:
            return True
        try:
            cam.set(name, value)
            report["changed"].append(pretty or name)
            return True
        except Exception as exc:  # noqa: BLE001
            report["skipped"].append(f"{pretty or name}: {exc}")
            return False

    modes = props[PROPS["exposure_mode"]].choices
    if preset.mode and preset.mode in modes:
        try_set("exposure_mode", preset.mode, "mode → S&Q M (slow motion)" if preset.mode == MODE_SQ_M else "mode")
    elif preset.movie and MODE_MOVIE_M in modes:
        try_set("exposure_mode", MODE_MOVIE_M, "mode → Movie M")
    elif MODE_M in modes:
        try_set("exposure_mode", MODE_M, "mode → M")
    time.sleep(0.5)
    props = cam.refresh()

    # Picture Profile: photo presets use normal colours (off); video looks may pick a film profile.
    pp = (look or {}).get("picture_profile_video") if preset.movie else None
    if pp is None or preset.log_over is not None:      # LOG presets always keep their log profile
        pp = preset.picture_profile
    if pp is None:
        pp = 0          # never inherit S-Log (flat, noisy picture) from an earlier preset
    p = props.get(PROP_PICTURE_PROFILE)
    if pp is not None and p is not None and pp in (p.choices or []):
        try_set(PROP_PICTURE_PROFILE, pp, f"picture profile → {PP_NAMES.get(pp, pp)}")

    # In-camera colour for looks: white-balance shift (amber/blue, green/magenta).
    shift = (look or {}).get("wb_shift")
    for code, delta in ((PROP_WB_SHIFT_AB, (shift or {}).get("ab", 0)), (PROP_WB_SHIFT_GM, (shift or {}).get("gm", 0))):
        p = props.get(code)
        if p is not None and p.range:          # no look -> back to neutral (192)
            lo, hi, step = p.range
            val = int(min(max(192 + delta - (delta % (step or 1)), lo), hi))
            try_set(code, val, "WB shift " + ("amber/blue" if code == PROP_WB_SHIFT_AB else "green/magenta"))
    props = cam.refresh()
    if preset.focus and FOCUS[preset.focus] in props[PROPS["focus_mode"]].choices:
        try_set("focus_mode", FOCUS[preset.focus], f"focus → {preset.focus}")

    # Looks keep Auto white balance (an absolute Kelvin value would turn indoor
    # scenes orange) and colour the image with the in-camera WB shift + grade.
    # Only presets made for a specific light (Golden hour) fix the Kelvin.
    kelvin = preset.kelvin
    wb = "kelvin" if kelvin else (preset.wb or "auto")
    if wb and WB[wb] in props[PROPS["white_balance"]].choices:
        if try_set("white_balance", WB[wb], f"white balance → {wb}") and kelvin:
            props = cam.refresh()
            p = props.get(PROP_COLOR_TEMP)
            if p is not None and p.range:
                lo, hi, step = p.range
                kelvin = int(min(max(round(kelvin / (step or 100)) * (step or 100), lo), hi))
            try_set(PROP_COLOR_TEMP, kelvin, f"colour temperature → {kelvin} K")
    if not preset.movie:                    # single shot (not burst / bracket / self-timer)
        d = props.get(PROP_DRIVE)
        if d is not None and DRIVE_SINGLE in (d.choices or []):
            try_set("drive_mode", DRIVE_SINGLE, "drive → single shot")
    ev_extra = (look or {}).get("ev_bias", 0.0)

    first = None
    for _ in range(3):
        time.sleep(0.7)
        level, hi, _lo = brightness(cam.live_view())
        first = first or (level, hi)
        props = cam.refresh()
        want = solve(preset, props, level, ev_extra)
        if all(props[PROPS[k]].value == v for k, v in want.items()):
            break
        for k, v in want.items():
            try_set(k, v, f"{k.replace('_', ' ')} → {label(k, v)}")
        props = cam.refresh()

    time.sleep(0.7)
    level, hi, _lo = brightness(cam.live_view())
    props = cam.refresh()
    report["scene"] = {"before": round(first[0]), "described": describe(first[0]),
                       "after": round(level), "clipped_pct": round(hi, 1)}
    report["result"] = {k: label(k, props[PROPS[k]].value)
                        for k in ("exposure_mode", "aperture", "shutter_speed", "iso", "white_balance", "focus_mode")
                        if PROPS[k] in props}
    report["changed"] = list(dict.fromkeys(report["changed"]))
    # Verify: read everything back and compare with what the preset decided.
    ok, bad = [], []
    for code, (pretty, value) in intended.items():
        p = props.get(code)
        if p is None:
            continue
        (ok if p.value == value else bad).append(pretty.split(" → ")[0])
    report["verified"] = len(ok)
    report["mismatch"] = list(dict.fromkeys(bad))
    iso_now = props[PROPS["iso"]].value if PROPS["iso"] in props else None
    if preset.log_over is not None:
        under = (level - log_target(preset.log_over + ev_extra)) / SLOG3_STOP
        report["log"] = {"stops_over_grey": round((level - SLOG3_GREY) / SLOG3_STOP, 1),
                         "target_stops": preset.log_over}
    else:
        target = preset.target * (2 ** ((preset.ev_bias + ev_extra) / 2.2))
        under = math.log2(_lin(level) / _lin(target))
    if under < -1 and preset.log_over is not None:
        report["warning"] = (f"Too dark for S-Log: {abs(round(under, 1))} stops below the target even at "
                             "this ISO. Under-exposed 8-bit log gets noisy and bands. Add light, or use "
                             "HLG or a normal profile in this light.")
    elif under < -1:   # more than a stop under this preset's target
        report["warning"] = ("Still dark at this preset's limits. Add light, or pick Low light, "
                             "which accepts a slower shutter and more ISO.")
    elif hi > 3:
        report["warning"] = "Some highlights are clipping; try a darker look or reduce the light."
    if isinstance(iso_now, int) and 6400 < iso_now < 0xFFFFFF:
        report["noise"] = f"ISO {iso_now}: expect visible grain. More light is the real fix."
    return report
