"""A simulated Sony ZV-E10 built from the allowed-value lists recorded from the real camera (2026-11-11).

Recorded (tests/data/zve10_video_pp8_props.json, Movie M + PP8): aperture f/3.5-f/22 in 1/3 stops (kit zoom at
the wide end), shutter 1/4-1/4000 s, ISO 500-32000 with PP8, movie setting 4K 25p 100M/60M.
Measured today: photo mode with the profile off allows ISO 100-32000; PP8 in photo raises the floor to 500.
Quirks modelled: separate photo / video memories (exposure, focus, picture profile); video comes up in AF-C Wide;
writes of values outside the allowed list are refused (raise); live view renders the scene through the current
exposure, in S-Log3 when PP7/8/9 is on, else a Rec.709-like camera picture.
Gaps (to capture with "Run camera check"): the photo-mode shutter list (long exposures are assumed), the
aperture range at the long end of the zoom, the Access-Denied live-view retry and the wrong rec state after start.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

from camctl import presets
from camctl.drivers.sony import PROPS, PropInfo

_DATA = Path(__file__).parent / "data"


def _load(name):
    return {int(d["code"], 16): d for d in json.loads((_DATA / name).read_text(encoding="utf-8-sig"))}


_VID = _load("zve10_video_pp8_props.json")          # recorded on the real camera: Movie M, PP8
_PHO = _load("zve10_photo_std_props.json")          # recorded on the real camera: Manual, profile off (camera check)


def _choices(code, dump=_VID):
    return [c["value"] for c in (dump.get(code) or {}).get("choices") or []]


APERTURES = _choices(PROPS["aperture"])
VIDEO_SHUTTERS = _choices(PROPS["shutter_speed"])
PHOTO_SHUTTERS = _choices(PROPS["shutter_speed"], _PHO)       # real: BULB (0), 30 s .. 1/4000
ISO_PP = [v for v in _choices(PROPS["iso"]) if v != 0xFFFFFF]                     # 500..32000 with a log profile
ISO_STD_ALL = [v for v in _choices(PROPS["iso"], _PHO) if v != 0xFFFFFF]          # real: incl. Multi Frame NR (0x1000xxxx)
ISO_STD = [v for v in ISO_STD_ALL if v < 0xFFFFFF]                                   # 100..32000
FOCUS_PHOTO = _choices(PROPS["focus_mode"], _PHO)                                    # real: AF-S, AF-A, AF-C, DMF, MF
FOCUS_VIDEO = _choices(PROPS["focus_mode"])                                           # real: AF-C, MF only
MOVIE_M, PHOTO_M = 0x78053, 1
PP = presets.PROP_PICTURE_PROFILE


class ZVE10:
    name = "ZV-E10 (simulated)"

    # INVENTED test models (not real cameras): other capability lists, to test the model-independent paths
    SIM_MODELS = {
        "SIM-A": {"name": "SIM-A (simulated)", "pp": list(range(0, 8)), "iso": (100, 25600), "zoom": False, "lv": (256, 144)},
        "SIM-FX": {"name": "SIM-FX (simulated)", "pp": None, "iso": (800, 102400), "zoom": True, "lv": (480, 270)},
        "SIM-B": {"name": "SIM-B (simulated)", "pp": list(range(0, 11)), "iso": None, "zoom": False, "lv": (320, 180),
                  "focus_indicator": True},
        # other FORMATS: JPEG / RAW + JPEG only, the aspect ratio readable but not settable, profiles 0-3 only,
        # HD video only at 30p and 60p
        "SIM-C": {"name": "SIM-C (simulated)", "pp": [0, 1, 2, 3], "iso": (100, 25600), "zoom": False, "lv": (320, 180),
                  "still": [2, 3], "aspect_fixed": True, "movie_formats": [9], "hd_settings": [1, 2, 0x18, 0x1A]},
    }
    # Movie record settings per file format. 4K: recorded on his camera (4K 25p 100M / 60M). HD: ASSUMED for the
    # simulator (the PAL list in Sony's numbering); to confirm on the camera.
    MOVIE_SETTINGS = {8: [0x21, 0x24], 9: [4, 5, 0x19, 0x1B, 0x1D, 0x1F]}
    ASPECT_CODE = 0xD211

    def __init__(self, ev=10.0, person=True, highlight=0.0, backlight=0.0, mode="photo", battery=80, model=None):
        self.ev = ev                   # scene brightness at the subject, EV100 (sun 15, shade 12, room 6...)
        self.person = person
        self.highlight = highlight     # a bright patch (window / lamp) this many stops above the subject
        self.backlight = backlight     # background this many stops brighter than the subject
        self.writes: list[tuple[int, int]] = []
        self.refused: list[tuple[int, int]] = []
        self.frames = 0
        e = lambda code, val, ch, dt=6: PropInfo(code, dt, True, True, val, choices=list(ch))
        common = {
            PROPS["white_balance"]: e(PROPS["white_balance"], 2, [2, 4, 6, 32786], 4),
            0xD20F: PropInfo(0xD20F, 4, True, True, 5600, range=(2500, 9900, 100)),
            presets.PROP_WB_SHIFT_AB: PropInfo(presets.PROP_WB_SHIFT_AB, 4, True, True, 192, range=(164, 220, 2)),
            presets.PROP_WB_SHIFT_GM: PropInfo(presets.PROP_WB_SHIFT_GM, 4, True, True, 192, range=(164, 220, 1)),
            0xD21B: e(0xD21B, 0x8000, [0x8000, 0x8001]), 0xD201: e(0xD201, 1, [1]),
            PROPS["drive_mode"]: e(PROPS["drive_mode"], 1, [1], 4),
            0xD241: e(0xD241, 8, [8, 9]), 0xD242: e(0xD242, 33, [33, 36]),
            0xD253: e(0xD253, 2, [1, 2, 3]), 0xD25F: e(0xD25F, 3, [1, 3, 4]), 0xD203: e(0xD203, 1, [1, 2, 3]),
            0xD218: PropInfo(0xD218, 3, False, True, battery, range=(-1, 100, 1)),
            0xD25D: PropInfo(0xD25D, 6, False, True, 0x01000000),          # power zoom position (0 wide .. 100 tele)
            0xD21D: PropInfo(0xD21D, 2, False, True, 0),                   # recording flag (1 = recording)
            0xD24A: PropInfo(0xD24A, 6, False, True, 4454),                # record time left on the card (s)
        }
        self.zoom_pos, self.zoom_dir, self.zoom_t = 0.0, 0, 0.0
        self.mem = {
            "photo": {PROPS["aperture"]: e(PROPS["aperture"], 800, APERTURES, 4),
                      PROPS["shutter_speed"]: e(PROPS["shutter_speed"], (1 << 16) | 125, PHOTO_SHUTTERS),
                      PROPS["iso"]: e(PROPS["iso"], 100, ISO_STD_ALL + [0xFFFFFF]),
                      PROPS["focus_mode"]: e(PROPS["focus_mode"], 2, FOCUS_PHOTO, 4),
                      0xD22C: e(0xD22C, 259, [1, 2, 259]),
                      PP: e(PP, 0, list(range(0, 11)), 2)},
            "video": {PROPS["aperture"]: e(PROPS["aperture"], 350, APERTURES, 4),
                      PROPS["shutter_speed"]: e(PROPS["shutter_speed"], (1 << 16) | 50, VIDEO_SHUTTERS),
                      PROPS["iso"]: e(PROPS["iso"], 800, ISO_PP + [0xFFFFFF]),
                      PROPS["focus_mode"]: e(PROPS["focus_mode"], 32772, FOCUS_VIDEO, 4),     # AF-C ...
                      0xD22C: e(0xD22C, 1, [1, 2, 259]),                                       # ... Wide
                      PP: e(PP, 8, list(range(0, 11)), 2)},
        }
        self.common = common
        self.p_mode = e(PROPS["exposure_mode"], MOVIE_M if mode == "video" else PHOTO_M,
                        [65538, 131075, 196612, 1, 0x78050, 0x78051, 0x78052, MOVIE_M])
        self.iso_range = None
        self.refuse: dict = {}            # code -> values the camera answers "not now" to although it lists them
        self.common[self.ASPECT_CODE] = e(self.ASPECT_CODE, 2, [1, 3, 2, 4])     # recorded: 16:9 set, 3:2 / 4:3 / 1:1
        if model:
            spec = self.SIM_MODELS[model]
            self.name = spec["name"]
            self.lv_size = spec["lv"]
            self.iso_range = spec["iso"]
            if not spec["zoom"]:
                self.common.pop(0xD25D, None)
            if spec.get("still"):
                self.common[0xD253] = e(0xD253, spec["still"][0], spec["still"])
            if spec.get("aspect_fixed"):
                self.common[self.ASPECT_CODE] = PropInfo(self.ASPECT_CODE, 2, False, True, 1, choices=[1])
            if spec.get("movie_formats"):
                mf = spec["movie_formats"]
                self.common[0xD241] = e(0xD241, mf[0], mf)
                self.MOVIE_SETTINGS = {**self.MOVIE_SETTINGS, 9: spec.get("hd_settings") or self.MOVIE_SETTINGS[9]}
                self.common[0xD242].value = self.MOVIE_SETTINGS[mf[0]][0]
            for m in self.mem.values():
                if spec["pp"] is None:
                    m.pop(PP, None)
                else:
                    m[PP].choices = list(spec["pp"])
                    if m[PP].value not in m[PP].choices:
                        m[PP].value = 0

    # -- the camera's view of its properties ------------------------------------------------
    @property
    def mode(self) -> str:
        return "video" if self.p_mode.value in (0x78050, 0x78051, 0x78052, MOVIE_M) else "photo"

    # -- power zoom like his E PZ 16-50: one speed, about 1.9 s end to end, the widest aperture closes f/3.5 -> f/5.6
    ZOOM_RATE = 100 / 1.9

    def _zoom_tick(self):
        import time as _t
        if self.zoom_dir:
            now = _t.time()
            self.zoom_pos = max(0.0, min(100.0, self.zoom_pos + self.zoom_dir * (now - self.zoom_t) * self.ZOOM_RATE))
            self.zoom_t = now
        if 0xD25D in self.common:                                 # a model without power zoom has no position
            self.common[0xD25D].value = (2 if self.mode == "video" else 1) << 24 | int(round(self.zoom_pos))
        widest = 350 if self.zoom_pos < 10 else 400 if self.zoom_pos < 40 else 500 if self.zoom_pos < 60 else 560
        for m in self.mem.values():
            ap = m[PROPS["aperture"]]
            want = getattr(ap, "wanted", None) or ap.value            # like the real body: it keeps the chosen aperture
            ap.wanted = want
            ap.choices = [v for v in APERTURES if v >= widest]
            ap.value = max(want, widest)                              # and only limits it while zoomed in

    ZOOM_COAST = 8          # like his lens: it runs on about 8 positions after the stop (measured 12 Nov 2026)

    def zoom(self, direction):
        import time as _t
        self._zoom_tick()
        new = 1 if direction > 0 else -1 if direction < 0 else 0
        if new == 0 and self.zoom_dir:
            self.zoom_pos = max(0.0, min(100.0, self.zoom_pos + self.zoom_dir * self.ZOOM_COAST))
        self.zoom_dir = new
        self.zoom_t = _t.time()

    def zoom_position(self):
        self._zoom_tick()
        return int(round(self.zoom_pos))

    @property
    def p(self) -> dict:
        self._zoom_tick()
        d = {PROPS["exposure_mode"]: self.p_mode, **self.common, **self.mem[self.mode]}
        if getattr(self, "focus_at_moves", False):         # focus position 0 near .. 100 far (only for the pull tests)
            d[0xD24C] = PropInfo(0xD24C, 2, False, True, int(getattr(self, "focus_pos", 0)))
        ms, mf = d.get(0xD242), d.get(0xD241)
        if ms is not None and mf is not None:                    # the rate list follows the file format
            ms.choices = list(self.MOVIE_SETTINGS.get(mf.value, ms.choices))
            if ms.value not in ms.choices:
                ms.value = ms.choices[0]
        iso = d[PROPS["iso"]]
        log = PP in d and d[PP].value in (7, 8, 9)
        # recorded: log 500 and up (both modes), photo without log 100 and up. ASSUMED: video without a log profile
        # (Off, HLG, Cine) also from 100 (not measured on his camera yet)
        iso.choices = (ISO_PP if log else (ISO_STD_ALL if self.mode == "photo" else ISO_STD)) + [0xFFFFFF]
        if getattr(self, "iso_range", None):
            lo, hi = self.iso_range
            iso.choices = sorted({v for v in iso.choices if lo <= v <= hi or v == 0xFFFFFF} | {lo, hi})
        if iso.value not in iso.choices:                          # the camera clamps to its floor
            iso.value = min(v for v in iso.choices if v != 0xFFFFFF)
        return d

    # recording like his ZV-E10: the movie button is a TOGGLE; the REC flag can read wrong for a moment (12 Nov 2026:
    # it read "not recording" about 8 s into a take). rec_flaky = windows (seconds after start, length) of a wrong flag.
    rec_flaky: list = []
    recordings_started = 0

    def _rec_tick(self):
        import time as _t
        if 0xD21D not in self.common:
            return
        now = _t.time()
        rec = getattr(self, "recording", False)
        left = getattr(self, "_left_base", 4454.0)
        if rec:
            left -= now - self._rec_t0
        flag = 1 if rec else 0
        if rec and any(a <= now - self._rec_t0 < a + d for a, d in self.rec_flaky):
            flag = 0                                      # the flaky flag
        if not rec and getattr(self, "_rec_t1", 0) and now - self._rec_t1 < 1.0 and self.rec_flaky:
            flag = 1                                      # and right after a stop
        self.common[0xD21D].value = flag
        self.common[0xD24A].value = int(left)

    def refresh(self):
        self._rec_tick()
        return dict(self.p)

    def set(self, prop, value):
        code = PROPS.get(prop, prop) if isinstance(prop, str) else prop
        p = self.p.get(code)
        if p is None:
            raise ValueError(f"unknown property 0x{code:04X}")
        ok = (value in p.choices) if p.choices else (p.range and p.range[0] <= value <= p.range[1])
        if not p.writable or value in self.refuse.get(code, ()):
            ok = False
        if not ok:
            self.refused.append((code, value))
            raise ValueError(f"camera refused 0x{code:04X}={value}")
        p.value = value
        if code == PROPS["aperture"]:
            p.wanted = value
        self.writes.append((code, value))

    def autofocus(self, hold=0.5):
        """A half press only (never the shutter)."""
        self.half_presses = getattr(self, "half_presses", 0) + 1

    def try_set(self, prop, value):
        try:
            self.set(prop, value)
            return 0x2001
        except ValueError:
            return 0x2005

    def focus_at(self, x, y, max_wait=3.0, lock_in_s1=False):
        self.focused = (x, y)
        if getattr(self, "focus_at_moves", False):                # INVENTED (focus pull tests): left = near, right = far
            self.focus_pos = int(round(min(max(x, 0), 1) * 100))
        return {"acquired": self.p[PROPS["focus_mode"]].value != 1, "indication": [1, 2, 2], "ms_af": 600}

    def capture(self, timeout=20.0, af=True):
        """Simulated shot: the real camera needs the half press held S1_SETTLE_S first (see the driver)."""
        import time as _t
        from camctl.drivers.sony import S1_SETTLE_S
        _t.sleep(S1_SETTLE_S + 0.4)
        self.shots = getattr(self, "shots", 0) + 1
        return [(0x3801, self.photo())]

    def photo(self):
        """A still: the live-view scene at 960x540 with sensor noise that grows with ISO (so averaging shows)."""
        import numpy as np
        im = Image.open(io.BytesIO(self.live_view())).resize((960, 540), Image.BILINEAR)
        a = np.asarray(im).astype(np.float32)
        yy, xx = np.mgrid[0:540, 0:960]
        a += (((xx // 24 + yy // 24) % 2) * 10 - 5)[..., None]      # some fine texture to align on
        iso = self.p[PROPS["iso"]].value
        sigma = 1.5 + 2.2 * max(0.0, math.log2(max(iso, 100) / 100))  # ISO 100 ≈ 1.5, ISO 6400 ≈ 15
        a += np.random.default_rng().normal(0, sigma, a.shape)
        b = io.BytesIO()
        Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(b, "JPEG", quality=95)
        return b.getvalue()

    def device_info(self):
        """Invented DeviceInfo, including things that must never reach a report (serial number)."""
        return {"standard_version": "1.00", "vendor_extension": "0x00000011 v100", "vendor_extension_desc": "sony.net",
                "functional_mode": 0, "operations": ["0x1001", "0x1002", "0x9201", "0x9202"], "events": ["0xC201"],
                "device_properties": ["0x5007", "0x500A"], "capture_formats": ["0x3801"], "image_formats": ["0x3801", "0xB101"],
                "manufacturer": "Sony Corporation", "model": self.name.split(" (")[0], "firmware": "9.99",
                "serial_number": "SIMSERIAL000123"}

    def focus_step(self, step):
        self.focus_pos = getattr(self, "focus_pos", 0) + int(step)
        self.focus_steps = getattr(self, "focus_steps", []) + [int(step)]

    def movie_record(self, start):
        import time as _t
        self._rec_tick()
        was = getattr(self, "recording", False)
        if was:                                           # the button stops: the card time stands still from here
            self._left_base = getattr(self, "_left_base", 4454.0) - (_t.time() - self._rec_t0)
            self._rec_t1 = _t.time()
        else:
            self._rec_t0 = _t.time()
            self.recordings_started += 1
        self.recording = not was

    # -- light ----------------------------------------------------------------------------
    def stops(self) -> float:
        """Exposure of the subject relative to 'metered' (0 = an 18% grey subject renders at 18%)."""
        p = self.p
        n = p[PROPS["aperture"]].value / 100
        t = presets.shutter_seconds(p[PROPS["shutter_speed"]].value) or 1 / 50
        iso = p[PROPS["iso"]].value
        cam_ev = math.log2(n * n / t) - math.log2(iso / 100)
        return self.ev - cam_ev

    def _encode(self, lin: float) -> int:
        pp = self.p.get(PP)
        if pp is not None and pp.value == 7 and self.name.startswith("ZV-E10"):     # S-Log2 (Sony's formula)
            y = 0.432699 * math.log10(155 * max(lin, 0.0) / 219 + 0.037584) + 0.646596
            return int(max(0, min(1, y * 0.8563 + 0.0626)) * 255 + 0.5)
        if pp is not None and pp.value in (7, 8, 9):            # S-Log3
            v = (420 + 261.5 * math.log10((lin + 0.01) / 0.19)) / 1023 if lin >= 0.01125 \
                else (lin * (171.2102946929 - 95) / 0.01125 + 95) / 1023
            return int(max(0, min(1, v)) * 255 + 0.5)
        lin = max(0.0, min(1.0, lin))                            # camera picture, clipped at white
        v = 4.5 * lin if lin < 0.018 else 1.099 * lin ** 0.45 - 0.099
        return int(v * 255 + 0.5)

    FACE = (0.38, 0.2, 0.24, 0.42)

    def live_view(self):
        self.frames += 1
        k = 2 ** self.stops()
        W, H = getattr(self, "lv_size", (320, 180))
        asp = self.common.get(self.ASPECT_CODE)
        if self.mode == "photo" and asp is not None and asp.value in (1, 3):
            # ASSUMED (never seen on his camera, it stays on 16:9): in 3:2 and 4:3 the live view arrives in the
            # recorded shape; in 1:1 it stays 16:9 and the camera's screen masks the sides
            W = int(round(H * (1.5 if asp.value == 1 else 4 / 3)))
        im = Image.new("RGB", (W, H), (self._encode(0.18 * k * 2 ** self.backlight),) * 3)
        d = ImageDraw.Draw(im)
        if self.highlight:
            d.rectangle((0, 0, 60, 70), fill=(self._encode(0.18 * k * 2 ** self.highlight),) * 3)
        x, y, w, h = self.FACE
        z = 1 + getattr(self, "zoom_pos", 0) / 100 * (50 / 16 - 1)       # 16 -> 50 mm
        cx, cy = x + w / 2, y + h / 2
        w, h = min(0.95, w * z), min(0.95, h * z)
        x, y = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
        skin = 0.36 * k
        c = (self._encode(skin * 1.1), self._encode(skin * 0.95), self._encode(skin * 0.85))
        d.rectangle((int(x * W), int(y * H), int((x + w) * W), int((y + h) * H)), fill=c)
        if getattr(self, "split", 0):              # key light from picture left: the right half of the face is darker
            s2 = skin * 2 ** -self.split
            c2 = (self._encode(s2 * 1.1), self._encode(s2 * 0.95), self._encode(s2 * 0.85))
            d.rectangle((int((x + w / 2) * W), int(y * H), int((x + w) * W), int((y + h) * H)), fill=c2)
        im.putpixel((self.frames % W, H - 1), (255, 0, 0))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95)
        return b.getvalue()

    def face_box_px(self, w, h):
        x, y, fw, fh = self.FACE
        return [(int(x * w), int(y * h), int(fw * w), int(fh * h))] if self.person else []
