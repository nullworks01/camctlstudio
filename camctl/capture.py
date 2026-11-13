"""Capture profiles: a technically perfect, NEUTRAL capture for grading in Resolve / Lightroom.

Studio never colours files. Every profile:
  - format: his choice in All settings › Camera › Format (formats.py), from what the camera offers; Studio's
    choice (the default): photos RAW (+ a JPEG reference, Extra fine, L); video S-Log3 (PP8), 4K at the best bitrate
  - neutral colour: WB shift centred (192/192), picture effect off; never a creative look
  - white balance: locked (measured from a grey card / white paper) or AWB flagged "not locked"
  - shutter: video 180-degree rule for the frame rate; photo a safe handheld speed for the subject
  - aperture: chosen for the subject, inside the lens's range
  - exposure BY MEASUREMENT on the live frame: video skin +1.5 stops over grey in S-Log3, highlights
    protected; photo as bright as possible without clipping skin or important highlights (ETTR),
    base ISO first, ISO last; above the noise guard it says "add light", too bright says "ND"
  - focus: eye/face AF (people) or AF-S zone (objects), confirmed sharp
  - read-back + a technical verdict (green/amber/red, the one thing to change)
"""
from __future__ import annotations

import math
import re
import time

from . import camprofile
from . import measure, presets
from .drivers.sony import PROPS

MOVIE_M, PHOTO_M = 0x78053, 1
PP_SLOG3, PP_OFF = 8, 0
STILL_RAW_JPEG = 2
WB_AUTO, WB_KELVIN = 2, 32786
AF_S, AF_C = 2, 32772
AREA_WIDE, AREA_ZONE, SPOT_L = 1, 2, 259
PROP_PICTURE_EFFECT = 0xD21B
PICTURE_EFFECT_OFF = 0x8000
NEUTRAL_SHIFT = 192
NOISE_GUARD = {"photo": 3200, "video": 6400}        # above this: visible noise (stated in the verdict)
ISO_CEILING = {"photo": 12800, "video": 12800}     # correct exposure first: ISO may go this high, as a stated cost

# properties that change colour in the recorded file: capture profiles may only set them to neutral
PROP_DRO, DRO_OFF = 0xD201, 1
COLOUR_PROPS = {presets.PROP_WB_SHIFT_AB: NEUTRAL_SHIFT, presets.PROP_WB_SHIFT_GM: NEUTRAL_SHIFT,
                PROP_PICTURE_EFFECT: PICTURE_EFFECT_OFF, PROP_DRO: DRO_OFF}     # DRO changes JPEG/video tones

PROFILES = {
    "interview": {"name": "Interview / talking head", "people": True,
                  "photo": {"f": 4.0, "min_shutter": 1 / 100, "open_to": 0.0, "slow_floor": 1 / 60}, "video": {"f": 4.0, "open_to": 0.0}},
    "portrait": {"name": "Portrait", "people": True,
                 "photo": {"f": 0.0, "min_shutter": 1 / 125, "slow_floor": 1 / 60}, "video": {"f": 0.0}},
    "product": {"name": "Product", "people": False,
                "photo": {"f": 8.0, "min_shutter": 1 / 60, "open_to": 5.6}, "video": {"f": 5.6, "open_to": 4.0}},
    "daylight": {"name": "Daylight exterior", "people": True,
                 "photo": {"f": 8.0, "min_shutter": 1 / 250, "open_to": 5.6, "slow_floor": 1 / 125}, "video": {"f": 5.6, "close_to": 8.0}},
    "lowlight": {"name": "Low light", "people": True,
                 "photo": {"f": 0.0, "min_shutter": 1 / 60}, "video": {"f": 0.0}},
}
ORDER = ["interview", "portrait", "product", "daylight", "lowlight"]


def catalog() -> list[dict]:
    return [{"key": k, "name": PROFILES[k]["name"]} for k in ORDER]


# ---------------------------------------------------------------- helpers
def _p(props, code):
    return props.get(code)


def _choices(props, code) -> list[int]:
    p = props.get(code)
    if p is None:
        return []
    if p.choices:
        return list(p.choices)
    if p.range:
        lo, hi, step = p.range
        return list(range(lo, hi + 1, step or 1))
    return []


def fps_of(movie_label: str) -> int:
    m = re.search(r"(\d+)p", movie_label or "")
    return int(m.group(1)) if m else 25


def video_shutter_s(fps: int) -> float:
    return 1 / {24: 50, 25: 50, 30: 60, 50: 100, 60: 125, 100: 200, 120: 250}.get(fps, 50)


def video_shutter(fps: int, hz: int = 50) -> tuple[float, bool]:
    """(shutter in seconds, flicker-safe) for the frame rate: about 1/(2 x fps), in step with the mains lights
    (a multiple of half the mains period) and never longer than one frame. 25p/50 Hz -> 1/50, 30p/50 Hz -> 1/50,
    60p/50 Hz -> 1/100, 100p/50 Hz -> 1/100, 25p/60 Hz -> 1/60, 50p/60 Hz -> 1/120. Above twice the mains
    frequency (120p under 50 Hz) no flicker-free shutter exists: 180 degrees, and the caller says so."""
    fps = int(fps or 25)
    ideal = 1 / (2 * fps)
    if not hz:
        return ideal, True
    period = 1 / (2 * hz)
    cands = [n * period for n in range(1, 13) if n * period <= 1 / fps * 1.001]
    if not cands:
        return ideal, False
    return min(cands, key=lambda t: (abs(math.log2(t / ideal)), t)), True


def best_movie_setting(choices: list[int], labels: dict[int, str], fps: int) -> int | None:
    """Highest bitrate at the current frame rate, 4K first."""
    def key(v):
        lab = labels.get(v, "")
        rate = int(re.search(r"(\d+)M", lab).group(1)) if re.search(r"(\d+)M", lab) else 0
        return ("4K" in lab, fps_of(lab) == fps, rate)
    good = [v for v in choices if v in labels]
    return max(good, key=key) if good else None


def nearest_f(choices: list[int], target: float) -> int | None:
    if not choices:
        return None
    if target <= 0:
        return min(choices)                        # wide open
    return min(choices, key=lambda v: abs(v / 100 - target))


def _curve(curve: str | None) -> tuple[float, float]:
    """(mid grey, one stop) in 0..255 code values for a log curve (S-Log3 unless the profile says S-Log2)."""
    from .formats import LOG_CURVES
    g, s = LOG_CURVES.get(curve or "slog3", LOG_CURVES["slog3"])
    return g * 255, s * 255


def stops_error(level: float, target: float, log: bool, curve: str | None = None) -> float:
    if log:
        return (target - level) / _curve(curve)[1]
    return math.log2(presets._lin(target) / presets._lin(max(level, 1.0)))


def target_level(log: bool, face: bool, photo: bool, curve: str | None = None, jpeg_only: bool = False) -> float:
    if log:
        grey, stop = _curve(curve)
        if photo:          # log photo preview: expose the RAW at nominal (skin +1 stop), no extra ISO for ETTR
            return grey + stop * (1.0 if face else 0.5)
        return grey + stop * (1.5 if face else 1.0)
    if photo and jpeg_only:
        return 150.0 if face else 118.0            # JPEG only: no RAW headroom, so skin ~59 IRE and highlights kept
    if photo:
        return 166.0 if face else 128.0            # ETTR: skin ~65 IRE (RAW keeps highlight headroom)
    return 158.0 if face else 118.0                # standard and HLG video: a normal picture


# ---------------------------------------------------------------- the run
class CaptureRun:
    """Runs a profile on the Studio camera step by step (between steps the live view keeps going)."""

    def __init__(self, studio, set_fn, values_fn, labels_fn, wb_lock=None, log=lambda s: None):
        """set_fn(name_or_code, value) applies (and logs) one setting; values_fn() -> {code: PropInfo};
        labels_fn(name, value) -> label; wb_lock: Kelvin or None."""
        self.studio, self.set, self.props, self.label = studio, set_fn, values_fn, labels_fn
        self.wb_lock, self.log = wb_lock, log
        self.intended: dict[int, int] = {}

    def profile(self) -> dict:
        return camprofile.of(self.studio)

    def _want(self, code: int, value: int) -> bool:
        props = self.props()
        p = props.get(code)
        if p is None or not p.writable:
            return False
        if value not in _choices(props, code) and not p.range:
            return False
        if p.value != value:
            try:
                self.set(code, value)
            except Exception as exc:  # noqa: BLE001 - e.g. "not changeable in the current mode": skip it
                self.log(f"skipped 0x{code:04X}: {exc}")
                return False
        self.intended[code] = value
        return True

    def _frame(self) -> bytes:
        seq = self.studio.frame_seq
        time.sleep(0.6)
        f, got = self.studio.latest_frame(seq + 2, timeout=3)
        return f if f is not None else self.studio.run(lambda c: c.live_view(), keep_cache=True)

    def formats(self, mode: str) -> list[str]:
        notes = []
        props = self.props()
        if mode == "video":
            self._want(PROPS["exposure_mode"], MOVIE_M)
            cls = self.props().get(0xD247)               # shooting mode class: 0 still, 1 movie
            em = self.props().get(PROPS["exposure_mode"])
            if (em is not None and em.value != MOVIE_M) or (cls is not None and cls.value == 0 and em is not None and em.value != MOVIE_M):
                # the camera is not in movie mode (he switched back meanwhile): never write video settings into photo
                notes.append("camera is not in movie mode: video settings not written")
                return notes
            prof = self.profile()
            from . import formats as fm
            photo_log = getattr(self, "photo_log", None) is True
            want = lambda key: fm.wanted("video", key, self.props(), prof, self.label, photo_log)
            pp_v, pp_note = want("picture_profile")
            if pp_note:
                notes.append(pp_note)
            if pp_v is None:
                if fm.chosen("video", "picture_profile") is None and fm._log_pp(prof) is None:
                    # an untested model: never assume which picture profile is log
                    notes.append("Log profile not confirmed on this model: the picture profile was left as it is")
            elif not self._want(presets.PROP_PICTURE_PROFILE, pp_v):
                notes.append(f"{fm.pp_name(prof, pp_v)} is not available in this mode")
            for key in ("movie_format", "movie_setting"):      # the rate list depends on the file: in this order
                v, n = want(key)
                if n:
                    notes.append(n)
                if v is not None:
                    self._want(fm.CODES[key], v)
            fm.remember("video", self.props())
        else:
            if self.props()[PROPS["exposure_mode"]].value != PHOTO_M:
                self._want(PROPS["exposure_mode"], PHOTO_M)
                time.sleep(0.8)                      # photo-only settings unlock a moment after the mode change
            prof = self.profile()
            from . import formats as fm
            photo_log = getattr(self, "photo_log", None) is True
            want = lambda key: fm.wanted("photo", key, self.props(), prof, self.label, photo_log)
            sf, n = want("still_format")
            if n:
                notes.append(n)
            if sf is not None:
                for _ in range(3):
                    if self._want(fm.SP_STILL_FORMAT, sf):
                        break
                    time.sleep(0.6)
                p = self.props().get(fm.SP_STILL_FORMAT)
                if p is not None and p.value != sf:
                    notes.append(f"{fm.STILL.get(sf, 'This file type')} is not set: set the camera's File Format to "
                                 f"{fm.STILL.get(sf, 'it')} in the camera menu")
            for key in ("jpeg_quality", "image_size"):  # his pick, else Studio's choice: best quality, largest size
                v, n = want(key)
                if n:
                    notes.append(n)
                p = self.props().get(fm.CODES[key])
                if v is not None and p is not None and p.enabled:
                    self._want(fm.CODES[key], v)
                    self.intended.pop(fm.CODES[key], None)   # not a read-back failure if the camera keeps its own
            asp, n = want("aspect")                  # only by his choice: Studio's choice leaves the ratio alone
            if n:
                notes.append(n)
            if asp is not None:
                self._want(fm.SP_ASPECT, asp)
            # the photo picture profile only changes the preview / JPEG, never the RAW: his choice in Format (or
            # "Photo preview like video"); Studio's choice only sets a log profile left on from video back to normal
            pp, n = want("picture_profile")
            if n:
                notes.append(n)
            if pp is not None:
                cur = self.props().get(presets.PROP_PICTURE_PROFILE)
                if self._want(presets.PROP_PICTURE_PROFILE, pp) and cur is not None and cur.value != pp \
                        and pp == PP_OFF and fm.chosen("photo", "picture_profile") is None:
                    self.notes_pp = "photo preview set back to the normal picture (log was left on from video / a test)"
            fm.remember("photo", self.props())
            d = self.props().get(PROPS["drive_mode"])
            if d is not None and presets.DRIVE_SINGLE in (d.choices or []):
                self._want(PROPS["drive_mode"], presets.DRIVE_SINGLE)
        return notes

    def neutral_colour(self) -> None:
        for code, neutral in COLOUR_PROPS.items():
            p = self.props().get(code)
            if p is not None and p.writable and p.enabled and (neutral in _choices(self.props(), code) or p.range):
                self._want(code, neutral)

    def white_balance(self) -> bool:
        if self.wb_lock:
            if self._want(PROPS["white_balance"], WB_KELVIN):
                k = int(round(self.wb_lock / 100) * 100)
                self._want(0xD20F, k)
                return True
        self._want(PROPS["white_balance"], WB_AUTO)
        return False

    def lens_and_shutter(self, prof: dict, mode: str) -> None:
        props = self.props()
        spec = prof[mode]
        ap = nearest_f(_choices(props, PROPS["aperture"]), spec["f"])
        if ap is not None:
            self._want(PROPS["aperture"], ap)
        sh_choices = _choices(props, PROPS["shutter_speed"])
        if mode == "video":
            ms = props.get(0xD242)
            fps = fps_of(self.label("movie_setting", ms.value)) if ms else 25
            hz = getattr(self, "mains_hz", 50)               # no flicker under mains lights: shutter in step with it
            want, self.flicker_safe = video_shutter(fps, hz)
        else:
            want = spec["min_shutter"]
        best = min((v for v in sh_choices if presets.shutter_seconds(v)), default=None,
                   key=lambda v: abs(math.log2(presets.shutter_seconds(v) / want)))
        if best is not None:
            self._want(PROPS["shutter_speed"], best)

    def focus(self, prof: dict, mode: str, face_box=None, moving: bool = False, track_choice: bool = False) -> None:
        """Operator focus: decide the target, acquire once, verify, lock (MF), hold.
        Target: the eyes of the detected person, else the centre / object. Tracking (AF-C) only as a
        deliberate choice: the subject really moves toward or away from the camera, or the owner chose it."""
        props = self.props()
        modes = _choices(props, PROPS["focus_mode"])
        areas = _choices(props, 0xD22C)
        self.target = [face_box[0] + face_box[2] / 2, face_box[1] + face_box[3] * 0.4] if face_box else [0.5, 0.5]
        self.track = bool((track_choice or (moving and face_box)) and AF_C in modes)
        if self.track:
            self._want(PROPS["focus_mode"], AF_C)
            if AREA_WIDE in areas:
                self._want(0xD22C, AREA_WIDE)
            self.focus_text = "tracking your eyes, by choice" if track_choice else "tracking your eyes (you're moving)"
            return
        if AF_S in modes:
            self._want(PROPS["focus_mode"], AF_S)
        elif AF_C in modes:                                   # video on this body: AF-C to acquire, then MF lock
            self._want(PROPS["focus_mode"], AF_C)
            self.intended.pop(PROPS["focus_mode"], None)
        spot = next((a for a in (SPOT_L, 258, 257) if a in areas), None)
        if spot is not None:
            self._want(0xD22C, spot)
        self.focus_text = "your eyes, locked" if face_box else ("the object, locked" if not prof["people"] else "the centre, locked")

    def expose(self, prof: dict, mode: str, log: bool) -> dict:
        """Measure -> adjust, up to 6 rounds. Photo: shutter (not slower than safe) then ISO; video: ISO only."""
        spec = prof[mode]
        cap = getattr(self, "cap_override", None) or ISO_CEILING[mode]
        curve = getattr(self, "curve", None)                 # 'slog3' / 'slog2' when a log profile is active
        jpeg = mode == "photo" and getattr(self, "jpeg_only", False)
        last = {}
        # start from the format's base ISO (S-Log3 has a high base on this camera: read it from the choices)
        props0 = self.props()
        isos0 = sorted(c for c in props0[PROPS["iso"]].choices if 0 < c < 0xFFFFFF)
        if isos0:
            self._want(PROPS["iso"], isos0[0])
            self.base_iso = isos0[0]
        open_limit = spec.get("open_to", 0.0)        # how far the aperture may open for this shot (0 = wide open)
        for _ in range(10):                          # shutter, ISO, iris, slower shutter, ISO: room to converge
            frame = self._frame()
            m = measure.analyze(frame, log=log)
            face = m.get("face")
            has_face = bool(face and face.get("mean") is not None)
            level = (face["mean"] if has_face else m["centre_weighted_ire"]) * 2.55
            err = stops_error(level, target_level(log, has_face, mode == "photo", curve, jpeg), log, curve)
            clip = m["overall"]["clip_pct"]
            fclip = (face or {}).get("clip_pct", 0) or 0
            if jpeg and not has_face and clip > 0.3 and err > -0.3:
                err = min(err, -0.3 - min(clip, 10) / 10)   # JPEG only: no RAW to pull highlights back, keep them
            if fclip > 0.3 or (mode == "photo" and not has_face and clip > 1.0 and err > -0.3):
                # protect skin; without a face, the important highlights. With a face (backlight, a window
                # behind him) the face is exposed correctly and the verdict asks for fill light instead.
                err = min(err, -0.4 - min(clip, 10) / 10)
            if log:
                # S-Log3 guard: no face found (turned away, dark clothes in the centre) must not push the frame
                # hot; the brightest tenth stays at or below ~66 IRE, skin highlights at or below ~70 IRE
                grey, stop = _curve(curve)
                stop_ire = stop / 2.55
                shift = (grey - presets.SLOG3_GREY) / 2.55      # S-Log2 sits about 8 IRE lower than S-Log3
                p90 = (m.get("overall") or {}).get("p90")
                if p90 is not None and not has_face:
                    err = min(err, (66.0 + shift - p90) / stop_ire)
                fp90 = (face or {}).get("p90")
                if has_face and fp90 is not None:
                    err = min(err, (70.0 + shift - fp90) / stop_ire)
            prev_err = last.get("err")
            last = {"err": round(err, 2), "level": level, "face": has_face, "clip": clip, "face_clip": fclip,
                    "ire": round(level / 2.55)}
            if abs(err) < 0.3:
                break
            raw_err = err
            if prev_err is not None and prev_err * err < 0:
                err *= 0.5                     # damp: smaller steps after an overshoot, no ping-pong
            props = self.props()
            iso_p = props[PROPS["iso"]]
            isos = sorted(c for c in iso_p.choices if 0 < c < 0xFFFFFF and c <= cap)
            sh_p = props[PROPS["shutter_speed"]]
            moved = False
            if mode == "photo":
                shs = sorted((v for v in sh_p.choices if presets.shutter_seconds(v)), key=presets.shutter_seconds)
                cur_t = presets.shutter_seconds(sh_p.value) or spec["min_shutter"]
                cur_iso = iso_p.value if 0 < iso_p.value < 0xFFFFFF else (isos[0] if isos else 100)
                # brighter: lower ISO is never needed; prefer slower shutter down to the safe minimum, then ISO
                # darker: first bring ISO back to base, then a faster shutter
                if err > 0:
                    cand = [v for v in shs if presets.shutter_seconds(v) <= spec["min_shutter"] * 1.01]
                    t_want = cur_t * 2 ** err
                    t_best = max((v for v in cand if presets.shutter_seconds(v) <= t_want * 1.06), default=None,
                                 key=presets.shutter_seconds)
                    if t_best is not None and presets.shutter_seconds(t_best) > cur_t * 1.05:
                        self._want(PROPS["shutter_speed"], t_best)
                        moved = True
                    elif isos and "open_to" in spec and cur_iso >= NOISE_GUARD["photo"] * 0.89 \
                            and cur_iso * 2 ** err > NOISE_GUARD["photo"] * 1.12 \
                            and self._open_iris(props, spec, math.log2(cur_iso * 2 ** err / NOISE_GUARD["photo"])):
                        moved = True                   # at the clean-ISO limit: a wider aperture before noisy ISO
                    elif isos and "slow_floor" in spec and cur_iso >= NOISE_GUARD["photo"] * 0.89 \
                            and cur_iso * 2 ** err > NOISE_GUARD["photo"] * 1.12 \
                            and (t_slow := max((v for v in shs if presets.shutter_seconds(v) <= spec["slow_floor"] * 1.01
                                                and presets.shutter_seconds(v) <= t_want * 1.06), default=None,
                                               key=presets.shutter_seconds)) is not None \
                            and presets.shutter_seconds(t_slow) > cur_t * 1.05:
                        self._want(PROPS["shutter_speed"], t_slow)   # a still subject: slower shutter before noisy ISO
                        moved = True
                    elif isos:
                        i_want = cur_iso * 2 ** err
                        if ("open_to" in spec or "slow_floor" in spec) and cur_iso < NOISE_GUARD["photo"] * 0.89 \
                                and i_want > NOISE_GUARD["photo"]:
                            i_want = NOISE_GUARD["photo"]          # stop at the clean limit first, then decide
                        i_best = min(isos, key=lambda v: abs(math.log2(v / i_want)))
                        if i_best == cur_iso and raw_err >= 0.3:      # still under: take the next brighter step
                            i_best = next((v for v in isos if v > cur_iso), cur_iso)
                        if i_best != cur_iso:
                            self._want(PROPS["iso"], i_best)
                            moved = True
                else:
                    if isos and cur_iso > isos[0]:
                        i_want = max(isos[0], cur_iso * 2 ** err)
                        i_best = min(isos, key=lambda v: abs(math.log2(v / i_want)))
                        if i_best != cur_iso:
                            self._want(PROPS["iso"], i_best)
                            moved = True
                    if not moved:
                        t_want = cur_t * 2 ** err
                        t_best = min(shs, key=lambda v: abs(math.log2(presets.shutter_seconds(v) / t_want)))
                        if t_best != sh_p.value:
                            self._want(PROPS["shutter_speed"], t_best)
                            moved = True
            else:
                cur_iso = iso_p.value if 0 < iso_p.value < 0xFFFFFF else (isos[0] if isos else 800)
                if err > 0:                                  # darker than wanted: open the iris first, ISO last
                    aps = sorted(_choices(props, PROPS["aperture"]))
                    cur_ap = props[PROPS["aperture"]].value
                    limit = max(min(aps, default=cur_ap), int(open_limit * 100)) if open_limit else min(aps, default=cur_ap)
                    wider = [a for a in aps if limit <= a < cur_ap]
                    if wider:
                        want_ap = cur_ap / (2 ** (err / 2))
                        self._want(PROPS["aperture"], min(wider, key=lambda a: abs(a - want_ap)))
                        continue
                if isos:
                    i_want = cur_iso * 2 ** err
                    i_best = min(isos, key=lambda v: abs(math.log2(v / max(i_want, 1))))
                    if i_best == cur_iso and raw_err >= 0.3:          # still under: take the next brighter step
                        i_best = next((v for v in isos if v > cur_iso), cur_iso)
                    if i_best != cur_iso:
                        self._want(PROPS["iso"], i_best)
                        moved = True
                if not moved and err < 0 and spec.get("close_to"):
                    aps = sorted(_choices(props, PROPS["aperture"]))
                    cur_ap = props[PROPS["aperture"]].value
                    nxt = [a for a in aps if cur_ap < a <= spec["close_to"] * 100]
                    if nxt:
                        self._want(PROPS["aperture"], nxt[0])
                        moved = True
            if not moved:
                break
        return last

    def _open_iris(self, props, spec: dict, err: float) -> bool:
        aps = sorted(_choices(props, PROPS["aperture"]))
        cur_ap = props[PROPS["aperture"]].value
        lim = int(spec.get("open_to", 0) * 100)
        limit = max(min(aps, default=cur_ap), lim) if lim else min(aps, default=cur_ap)
        wider = [a for a in aps if limit <= a < cur_ap]
        if not wider:
            return False
        want_ap = cur_ap / (2 ** (err / 2))
        self._want(PROPS["aperture"], min(wider, key=lambda a: abs(a - want_ap)))
        return True

    def autofocus(self, prof: dict, face_box) -> None:
        """Focus once on the eyes (or the object in the centre), then lock it in manual focus."""
        if getattr(self, "track", False):
            return                                        # he chose tracking: the camera follows the eyes
        try:
            x, y = getattr(self, "target", [0.5, 0.5])
            self.studio.run(lambda c: c.focus_at(x, y))   # acquire once on the target
            time.sleep(0.6)
        except Exception:  # noqa: BLE001
            self.focus_text = (getattr(self, "focus_text", "") or "focus") + " (could not refocus: locked where it was)"
        try:
            props = self.props()
            if 1 in _choices(props, PROPS["focus_mode"]):
                self._want(PROPS["focus_mode"], 1)        # MF: focus stays exactly there, never left in AF-C
                self.intended.pop(PROPS["focus_mode"], None)
        except Exception:  # noqa: BLE001
            pass

    def readback(self) -> list[str]:
        props = self.props()
        return [hex(code) for code, v in self.intended.items() if props.get(code) is not None and props[code].value != v]


FAST_LENS_F = 1.8


def low_light(mode: str, shot: str | None, props, exp: dict, label, gear: str = "", fps: int = 25) -> dict | None:
    """Ranked options when the scene is under at the ISO limit, for the shot type."""
    err = exp.get("err", 0.0)
    if err <= 0.3:
        return None
    aps = sorted(_choices(props, PROPS["aperture"]))
    widest = aps[0] / 100 if aps else None
    cur_ap = props[PROPS["aperture"]].value / 100 if props.get(PROPS["aperture"]) else None
    iso_p = props.get(PROPS["iso"])
    isos = sorted(c for c in (iso_p.choices if iso_p else []) if 0 < c < 0xFFFFFF)
    base = isos[0] if isos else None
    first_light = (gear.strip().splitlines()[0].lstrip("-• ").strip() if gear.strip() else "a soft light")
    light_opt = (f"Add about {err:.1f} stop{'s' if err >= 1.5 else ''} of light: {first_light} about 1–1.5 m away, "
                 "45 degrees to one side, slightly above the eyes (or move closer to the window)")
    opts = []
    if mode == "photo" and shot == "product":
        strategy = "tripod, base ISO and a long exposure"
        need = None
        sh = props.get(PROPS["shutter_speed"])
        if sh is not None:
            from .presets import shutter_seconds
            t = shutter_seconds(sh.value) or 1 / 60
            iso_now = iso_p.value if iso_p and 0 < iso_p.value < 0xFFFFFF else (base or 100)
            need = t * 2 ** err * (iso_now / (base or iso_now))
        opts.append(f"Put the camera on a tripod, base ISO {base}, shutter about {('1/' + str(round(1 / need))) if need and need < 1 else f'{need:.1f} s' if need else 'longer'}, "
                    "and use the 2 s self-timer (or a remote) so pressing the button doesn't shake it")
        opts.append(light_opt)
    elif mode == "photo":
        strategy = "RAW at a higher ISO as a stated compromise (denoises well in Lightroom)"
        opts.append(light_opt)
        if widest and cur_ap and cur_ap > widest:
            opts.append(f"Open the aperture to f/{widest:g}")
        opts.append("Keep the slowest safe shutter for people (about 1/100) and hold steady; raise ISO in RAW only as far as needed")
    else:
        strategy = "keep the 180-degree shutter and the ISO limit; fix it with light"
        opts.append(light_opt)
        if widest and cur_ap and cur_ap > widest + 0.01:
            opts.append(f"Open the aperture from f/{cur_ap:g} to f/{widest:g} (+{2 * math.log2(cur_ap / widest):.1f} stops, shallower focus)")
        if widest and widest > FAST_LENS_F:
            opts.append(f"A faster lens: an f/{FAST_LENS_F:g} prime gains {2 * math.log2(widest / FAST_LENS_F):.1f} stops over this lens at f/{widest:g}")
        if fps >= 50:
            opts.append(f"Shoot 25p instead of {fps}p: the 1/50 shutter gains {math.log2(fps / 25):.0f} stop")
        nxt = [c for c in isos if iso_p and c > iso_p.value]
        if nxt:
            opts.append(f"Last resort: ISO {nxt[0]} (+{math.log2(nxt[0] / iso_p.value):.1f} stop, visibly more noise)")
    return {"shortfall": round(err, 1), "strategy": strategy, "options": opts, "widest_f": widest, "base_iso": base}


def verdict(mode: str, exp: dict, props, wb_locked: bool, focus_ok: bool | None, notes: list[str],
            mismatch: list[str], label, shot: str | None = None, gear: str = "", fps: int = 25,
            iso_cap: int | None = None, profile: dict | None = None, want: dict | None = None) -> dict:
    """Technical verdict for the picture: green/amber/red items and the one thing to change."""
    items = []

    def add(key, status, text, todo=""):
        items.append({"key": key, "status": status, "text": text, "todo": todo})

    from . import formats as fm
    pp = props.get(presets.PROP_PICTURE_PROFILE)
    prof = profile or camprofile.for_model("ZV-E10")
    want = want or {}
    log = pp is not None and camprofile.is_log(prof, pp.value)
    ms = props.get(0xD242)
    rate = fm.movie_setting_name(label("movie_setting", ms.value)) if ms else ""
    if mode == "video":
        want_pp = want.get("picture_profile", fm._log_pp(prof))
        if want_pp is None and fm.chosen("video", "picture_profile") is None:
            add("format", "warn", "Log profile not confirmed on this model",
                "Run All settings › Studio › Check this camera to find the log profile")
        else:
            name = fm.pp_short(prof, want_pp)
            ok = pp is not None and pp.value == want_pp
            add("format", "ok" if ok else "bad", f"{name} ✓" if ok else f"Not {name}",
                "" if ok else f"The camera didn't take {name} here: check the camera's mode")
        add("quality", "ok", (rate + " ✓") if ms else "Movie format")
    else:
        sf = props.get(0xD253)
        want_sf = want.get("still_format", prof.get("still_raw_jpeg", STILL_RAW_JPEG))
        ok = sf is not None and sf.value == want_sf
        kind = fm.pp_kind(prof, pp.value if pp is not None else None)
        pv = fm.pp_short(prof, pp.value) if pp is not None and pp.value else "camera standard"
        fname = fm.STILL.get(want_sf, "the chosen file type")
        add("format", "ok" if ok else "bad", (f"{fname} ✓" + (" (+ JPEG reference)" if want_sf == 2 else "") + f" · Preview: {pv}")
            if ok else f"Not {fname} · Preview: {pv}",
            "" if ok else f"The camera didn't take {fname} here: set File Format to {fname} in the camera menu")
        if sf is not None and sf.value == 3:
            add("jpeg", "ok", "JPEG only: there is no RAW to recover bright parts later, so Studio keeps the highlights safe")
    iso_p = props.get(PROPS["iso"])
    iso = iso_p.value if iso_p is not None else None
    err = exp.get("err", 0.0)
    cap = NOISE_GUARD[mode]
    if abs(err) < 0.4:
        add("exposure", "ok", f"Exposure ✓ ({exp.get('ire')} IRE {'on the face' if exp.get('face') else 'centre'})")
    elif err > 0:
        at_cap = iso is not None and iso >= cap
        add("exposure", "bad" if err > 1 else "warn", f"Under-exposed by {err:.1f} stops",
            "Add light on the subject (a soft key light or face the window)" if at_cap or mode == "video" else "Add light")
    elif mode == "video" and log and err >= -0.75 and not exp.get("clip", 0) > 0.5 and not exp.get("face_clip", 0) > 0.3:
        add("exposure", "ok", f"Exposure ✓ ({exp.get('ire')} IRE, {-err:.1f} stop on the bright side: fine for log)")
    else:
        nd = max(1, round(-err))
        sh = props.get(PROPS["shutter_speed"])
        t = presets.shutter_seconds(sh.value) if sh is not None else None
        keep = f"1/{round(1 / t)}" if t and t < 1 else "video"
        add("exposure", "bad" if err < -1 else "warn", f"Over-exposed by {-err:.1f} stops at base ISO",
            (f"ND filter of {'at least' if exp.get('clip', 0) > 5 or (exp.get('ire') or 0) >= 85 else 'about'} {nd} stop{'s' if nd > 1 else ''} (ND{2 ** nd}) to keep the {keep} shutter" if mode == "video"
             else f"Less light, or an ND filter of about {nd} stop{'s' if nd > 1 else ''}"))
    if exp.get("face_clip", 0) > 0.3 or exp.get("clip", 0) > 2:
        behind = exp.get("face") and exp.get("face_clip", 0) <= 0.3
        add("highlights", "bad" if exp.get("face_clip", 0) > 0.3 else "warn",
            f"Highlights clipping ({exp.get('clip', 0):.1f}% of the frame)" + (", behind you: face exposed correctly" if behind else ""),
            "Fill light on your face (or a reflector), or turn so the window is to the side" if behind
            else "Move or diffuse the brightest light / window")
    else:
        add("highlights", "ok", "Highlights protected ✓")
    if iso is not None and 0 < iso < 0xFFFFFF:
        iso_choices = sorted(c for c in (props.get(PROPS["iso"]).choices or []) if 0 < c < 0xFFFFFF)
        base = iso_choices[0] if iso_choices else iso
        lamp = min(iso_choices, key=lambda c: abs(math.log2(c / max(iso / 2.83, base)))) if iso_choices else base
        if iso <= base:
            add("noise", "ok", f"ISO {iso} (base) ✓")
        elif iso <= cap:
            add("noise", "ok", f"ISO {iso} ✓ (low noise)")
        else:
            ceiling = iso_cap or ISO_CEILING[mode]
            if err <= 0.4:
                head = f"Correct exposure at ISO {iso}. "
            elif iso >= ceiling:
                head = f"ISO {iso} (the ceiling), still not enough light. "
            else:
                head = f"ISO {iso}, still under. "
            add("noise", "warn", head
                + "Expect visible noise" + (" (RAW denoises well in Lightroom)." if mode == "photo" else "."),
                (f"One lamp on your face (about +1.5 stops) would bring this to about ISO {lamp}" if shot != "product"
                 else f"A lamp on the object (about +1.5 stops) would bring this to about ISO {lamp}"))
    if mode == "video":
        add("wb", "ok" if wb_locked else "bad", "White balance locked ✓ (baked into the video)" if wb_locked
            else "White balance on auto: video bakes it in and it may drift", "" if wb_locked else "Tap “Set white balance” to lock it")
    else:
        raw = (props.get(0xD253).value if props.get(0xD253) is not None else 2) in (1, 2)
        add("wb", "ok" if wb_locked else "warn", ("White balance locked ✓ (RAW: still adjustable in post)" if raw else
                                                  "White balance locked ✓") if wb_locked
            else ("White balance on auto (RAW can be fixed in post; lock it for consistent takes)" if raw else
                  "White balance on auto: a JPEG keeps it, so lock it for consistent photos"),
            "" if wb_locked else "Tap “Set white balance” to lock it")
    if mode == "photo" and shot in PROFILES and iso is not None and 0 < iso < 0xFFFFFF:
        sh = props.get(PROPS["shutter_speed"])
        t_now = presets.shutter_seconds(sh.value) if sh is not None else None
        t_norm = PROFILES[shot]["photo"]["min_shutter"]
        if t_now and t_now > t_norm * 1.1:
            gained = math.log2(t_now / t_norm)
            iso_choices = sorted(c for c in (props.get(PROPS["iso"]).choices or []) if 0 < c < 0xFFFFFF)
            alt = min(iso_choices, key=lambda c: abs(math.log2(c / (iso * 2 ** gained)))) if iso_choices else round(iso * 2 ** gained)
            add("shutter", "ok", f"1/{round(1 / t_now)} instead of 1/{round(1 / t_norm)} buys {gained:.1f} stop: ISO {iso} instead of {alt}",
                "Hold still (or use a tripod); for movement go back to 1/" + str(round(1 / t_norm)))
    ll = low_light(mode, shot, props, exp, label, gear, fps) if err > 0.3 else None
    if mode == "photo" and shot == "product" and iso is not None and NOISE_GUARD["photo"] < iso < 0xFFFFFF:
        # a still object never needs a noisy ISO: tripod, base ISO, longer shutter
        base_iso = min(c for c in (props.get(PROPS["iso"]).choices or [iso]) if 0 < c < 0xFFFFFF)
        sh = props.get(PROPS["shutter_speed"])
        t_now = presets.shutter_seconds(sh.value) if sh is not None else 1 / 60
        t_need = (t_now or 1 / 60) * iso / base_iso
        add("tripod", "warn", f"Product at ISO {iso}: use a tripod instead",
            f"Tripod, ISO {base_iso}, shutter about {('1/' + str(round(1 / t_need))) if t_need < 1 else f'{t_need:.1f} s'}, 2 s self-timer")
    if ll:
        add("lowlight", "bad" if ll["shortfall"] >= 1 else "warn", f"{ll['shortfall']:.1f} stop{'s' if ll['shortfall'] >= 1.5 else ''} short of light: {ll['strategy']}",
            ll["options"][0] if ll["options"] else "")
    if focus_ok is not None:
        add("focus", "ok" if focus_ok else "warn", "Focus confirmed ✓" if focus_ok else "Focus not confirmed",
            "" if focus_ok else ("Tap the object on the screen to focus there" if shot == "product"
                                 else "Look at the camera; tap your eyes on the screen to focus"))
    for n in notes:
        add("note", "warn", n)
    if mismatch:
        add("readback", "warn", "Some settings didn't stick: " + ", ".join(mismatch), "Check the camera's mode dial / menu")
    order = {"bad": 0, "warn": 1, "ok": 2}
    worst = sorted(items, key=lambda i: order[i["status"]])[0]
    if ll or any(i["key"] == "tripod" for i in items):     # the plan for the light is the thing to do
        items.sort(key=lambda i: 0 if i["key"] in ("tripod", "lowlight") else 1)
    return {"items": items, "status": worst["status"], "lowlight": ll,
            "todo": next((i["todo"] for i in sorted(items, key=lambda i: order[i["status"]]) if i["todo"]), ""),
            "ready": worst["status"] == "ok"}
