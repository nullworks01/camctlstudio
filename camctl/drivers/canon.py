"""Canon EOS / EOS R driver over USB (Canon's PTP extension). UNTESTED ON A REAL CANON.

Codes: libgphoto2 camlibs/ptp2 (ptp.h, ptp-pack.c, config.c, library.c), checked 14 Nov 2026; only the numbers and
the record layouts are used. Value encodings: Canon stores aperture, shutter and ISO as APEX-like steps of 1/8 stop
(aperture 0x08 = f/1.0, +8 per stop; shutter 0x38 = 1 s, +8 per stop faster; ISO 0x48 = 100, +8 per stop). Studio
converts these to its own units and snaps to the printed values (1/125, f/5.6, ISO 400).

Canon EOS cameras do not answer the standard property requests. They report every setting as a stream of events
(GetEvent 0x9116): "value changed" and "allowed values changed" records. Settings are written with
SetDevicePropValueEx 0x9110. Live view: EVF mode on, output to the PC, then GetViewFinderData 0x9153. The shutter is
RemoteReleaseOn/Off 0x9128/0x9129. Files are sent to the PC (capture destination "PC"), like Studio does on Sony.
Movie start / stop: the record status property 0xD1B8 (4 = start, 0 = stop), as Canon's own SDK names it.
"""
from __future__ import annotations

import math
import struct
import time

from ..ptpip import PTPError
from .ptpstd import (ISO_AUTO, OC_GET_OBJECT, S_APERTURE, S_BATTERY, S_EV, S_EXPOSURE_MODE, S_FOCUS_MODE, S_ISO,
                     S_SHUTTER, S_WB, PTPCamera, PropInfo, Unsupported, f_to_studio, iso_to_studio, seconds_to_studio,
                     studio_to_seconds)

CANON_VID = 0x04A9

OC_GET_OBJECT_INFO_EX = 0x9109
OC_SET_PROP_EX = 0x9110
OC_SET_REMOTE_MODE = 0x9114
OC_SET_EVENT_MODE = 0x9115
OC_GET_EVENT = 0x9116
OC_TRANSFER_COMPLETE = 0x9117
OC_KEEP_DEVICE_ON = 0x911D
OC_RELEASE_ON = 0x9128
OC_RELEASE_OFF = 0x9129
OC_VIEWFINDER = 0x9153
OC_DO_AF = 0x9154
OC_DRIVE_LENS = 0x9155
OC_AF_POINT = 0x9159          # ZoomPosition: x, y in live-view coordinates
OC_AF_CANCEL = 0x9160

DP_APERTURE = 0xD101
DP_SHUTTER = 0xD102
DP_ISO = 0xD103
DP_EV = 0xD104
DP_AE_MODE = 0xD105
DP_FOCUS_MODE = 0xD108
DP_WB = 0xD109
DP_KELVIN = 0xD10A
DP_BATTERY = 0xD111
DP_SHOTS_LEFT = 0xD11B
DP_CAPTURE_DEST = 0xD11C      # 4 = to the PC
DP_EVF_OUTPUT = 0xD1B0        # bit 1 (2) = PC
DP_EVF_MODE = 0xD1B1          # 1 = on
DP_RECORD = 0xD1B8            # 4 = recording, 0 = stopped

EC_OBJECT_ADDED = 0xC181
EC_REQUEST_TRANSFER = 0xC186
EC_PROP_VALUE = 0xC189
EC_PROP_LIST = 0xC18A
EC_OBJECT_ADDED_64 = 0xC1A7
EC_REQUEST_TRANSFER_64 = 0xC1A9

RC_NOT_READY = 0xA102
RC_BUSY = 0x2019
CAPTURE_TO_PC = 4

# Canon value -> Studio value; Studio uses the standard / Sony numbering
WB_TO_STUDIO = {0: 2, 1: 4, 2: 32784, 3: 6, 4: 5, 5: 7, 6: 1, 8: 32785, 9: 32786}
FOCUS_TO_STUDIO = {0: 2, 1: 32773, 2: 32772, 3: 1}               # One-Shot, AI Focus, AI Servo, MF
AE_TO_STUDIO = {0: 2, 1: 4, 2: 3, 3: 1, 4: 1}                      # P, Tv, Av, M, Bulb (shown as M)


def aperture_from_canon(v: int) -> int | None:
    return None if v in (0, 0xFF, 0xFFFF, 0xB0) else f_to_studio(2 ** ((v - 8) / 16))


def shutter_from_canon(v: int) -> int | None:
    if v == 0x0C:
        return 0                       # bulb
    if v in (0, 0xFF, 0xFFFF):
        return None                    # auto
    return seconds_to_studio(2 ** (-(v - 0x38) / 8))


def iso_from_canon(v: int) -> int | None:
    if v == 0:
        return ISO_AUTO
    if v in (0xFF, 0xFFFF):
        return None
    return iso_to_studio(100 * 2 ** ((v - 0x48) / 8))


def ev_from_canon(v: int) -> int:
    """Signed 1/8-stop steps (0x03 = +1/3, 0x04 = +1/2, 0x05 = +2/3) -> 1/1000 EV."""
    v = v - 256 if v > 127 else v
    sign, v = (-1 if v < 0 else 1), abs(v)
    whole, frac = divmod(v, 8)
    return sign * (whole * 1000 + {0: 0, 3: 333, 4: 500, 5: 667}.get(frac, round(frac * 125)))


def _ev_to_canon(ms: int) -> int:
    sign, ms = (-1 if ms < 0 else 1), abs(ms)
    whole, rest = divmod(ms, 1000)
    frac = min(((0, 0), (333, 3), (500, 4), (667, 5), (1000, 8)), key=lambda t: abs(t[0] - rest))[1]
    v = sign * (whole * 8 + frac)
    return v & 0xFF


def _inverse(fn, value, choices):
    """The Canon code whose Studio value is `value` (from the camera's own list, else computed)."""
    for c in choices:
        if fn(c) == value:
            return c
    return None


class CanonEOSCamera(PTPCamera):
    kind = "canon-eos"
    brand = "Canon"
    vendor_id = CANON_VID

    # Canon property -> (Studio id, Canon -> Studio, PTP datatype Studio shows)
    MAP = {
        DP_APERTURE: (S_APERTURE, aperture_from_canon, 4),
        DP_SHUTTER: (S_SHUTTER, shutter_from_canon, 6),
        DP_ISO: (S_ISO, iso_from_canon, 6),
        DP_EV: (S_EV, ev_from_canon, 3),
        DP_WB: (S_WB, WB_TO_STUDIO.get, 4),
        DP_FOCUS_MODE: (S_FOCUS_MODE, FOCUS_TO_STUDIO.get, 4),
        DP_AE_MODE: (S_EXPOSURE_MODE, AE_TO_STUDIO.get, 4),
        DP_KELVIN: (0xD20F, lambda v: v, 4),
    }
    READ_ONLY = {DP_FOCUS_MODE, DP_AE_MODE}        # the lens switch and the mode dial decide these on a Canon

    def __init__(self, host: str = "usb", client=None, vendor: int | None = None):
        super().__init__(host, client, vendor)
        self.values: dict[int, int] = {}            # Canon code -> current value
        self.lists: dict[int, list[int]] = {}       # Canon code -> allowed values
        self.added: list[tuple[int, int]] = []      # (handle, format) of new files the camera announced
        self.lv_on = False
        self.recording = False

    # -- session ---------------------------------------------------------------------------------------------------
    def _after_open(self) -> None:
        if not self.supports_op(OC_GET_EVENT):
            raise Unsupported("This Canon is not an EOS camera with remote control over USB (for example an older "
                              "PowerShot). Studio cannot control it.")
        self._check(self.client.transaction(OC_SET_REMOTE_MODE, [1]), "Canon remote mode")
        self._check(self.client.transaction(OC_SET_EVENT_MODE, [1]), "Canon event mode")
        self.poll_events()
        if DP_CAPTURE_DEST in self.values and self.values[DP_CAPTURE_DEST] != CAPTURE_TO_PC:
            self._set_canon(DP_CAPTURE_DEST, CAPTURE_TO_PC)

    def _before_close(self) -> None:
        if self.lv_on:
            self._set_canon(DP_EVF_OUTPUT, 1)
        self.client.transaction(OC_SET_EVENT_MODE, [0])
        self.client.transaction(OC_SET_REMOTE_MODE, [0])

    # -- events ----------------------------------------------------------------------------------------------------
    def poll_events(self) -> None:
        r = self.client.transaction(OC_GET_EVENT)
        if not r.ok:
            return
        self.parse_events(r.data)

    def parse_events(self, d: bytes) -> None:
        i = 0
        while i + 8 <= len(d):
            size, code = struct.unpack_from("<II", d, i)
            if size < 8 or code == 0:
                break
            rec = d[i:i + size]
            if code == EC_PROP_VALUE and len(rec) >= 16:
                prop, val = struct.unpack_from("<II", rec, 8)
                self.values[prop] = val
            elif code == EC_PROP_LIST and len(rec) >= 20:
                prop, _form, n = struct.unpack_from("<III", rec, 8)
                n = min(n, (len(rec) - 20) // 4)
                self.lists[prop] = list(struct.unpack_from(f"<{n}I", rec, 20))
            elif code in (EC_OBJECT_ADDED, EC_OBJECT_ADDED_64, EC_REQUEST_TRANSFER, EC_REQUEST_TRANSFER_64) \
                    and len(rec) >= 12:
                handle = struct.unpack_from("<I", rec, 8)[0]
                fmt = struct.unpack_from("<H", rec, 16)[0] if code in (EC_OBJECT_ADDED, EC_OBJECT_ADDED_64) \
                    and len(rec) >= 18 else 0x3801
                if handle not in [h for h, _ in self.added]:
                    self.added.append((handle, fmt))
            i += size

    # -- settings --------------------------------------------------------------------------------------------------
    def refresh(self) -> dict[int, PropInfo]:
        self.poll_events()
        for code, (sid, conv, dtype) in self.MAP.items():
            if code not in self.values:
                continue
            cur = conv(self.values[code])
            if cur is None:
                cur = 0
            choices = []
            for v in self.lists.get(code, []):
                s = conv(v)
                if s is not None and s not in choices:
                    choices.append(s)
            self._prop_cache[sid] = PropInfo(sid, dtype, code not in self.READ_ONLY, True, cur, choices=choices)
        if DP_BATTERY in self.values:
            # 0..4 on most EOS bodies (empty .. full); shown roughly in percent
            v = self.values[DP_BATTERY]
            self._prop_cache[S_BATTERY] = PropInfo(S_BATTERY, 3, False, True, min(100, int(v) * 25) if v < 5 else -1,
                                                   range=(-1, 100, 1))
        return self._prop_cache

    def _set_canon(self, code: int, value: int) -> None:
        data = struct.pack("<III", 12, code, value & 0xFFFFFFFF)
        r = self.client.transaction(OC_SET_PROP_EX, data_out=data)
        if r.code == RC_BUSY:
            time.sleep(0.3)
            r = self.client.transaction(OC_SET_PROP_EX, data_out=data)
        self._check(r, f"Canon set 0x{code:04X}")

    def set(self, prop, value: int) -> None:
        sid = self.resolve(prop)
        code = next((c for c, (s, _, _) in self.MAP.items() if s == sid), None)
        if code is None:
            raise Unsupported(f"Studio cannot change this setting (0x{sid:04X}) on a Canon.")
        if code in self.READ_ONLY:
            raise Unsupported("On a Canon this is set on the camera itself (mode dial or the AF/MF switch on the lens).")
        conv = self.MAP[code][1]
        if code == DP_EV:
            native = _ev_to_canon(int(value))
        elif code == DP_KELVIN:
            native = int(value)
        else:
            native = _inverse(conv, int(value), self.lists.get(code, []))
            if native is None and code == DP_WB:
                native = next((k for k, v in WB_TO_STUDIO.items() if v == value), None)
            if native is None and code == DP_ISO and value == ISO_AUTO:
                native = 0
            if native is None:
                raise PTPError("The camera does not offer that value right now.")
        self._set_canon(code, native)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            self.poll_events()
            if self.values.get(code) == native:
                break
            time.sleep(0.1)
        self.refresh()

    # -- live view -------------------------------------------------------------------------------------------------
    def _start_live_view(self) -> None:
        if self.values.get(DP_EVF_MODE, 0) != 1:
            r = self.client.transaction(OC_SET_PROP_EX, data_out=struct.pack("<III", 12, DP_EVF_MODE, 1))
            if not r.ok and r.code != RC_BUSY:
                raise PTPError(f"Canon live view could not be switched on (0x{r.code:04X})")
        out = self.values.get(DP_EVF_OUTPUT, 0)
        if not out & 2:
            self._set_canon(DP_EVF_OUTPUT, 2)
        self.lv_on = True

    def live_view(self) -> bytes:
        if not self.lv_on:
            self._start_live_view()
        deadline = time.time() + 3.0
        while True:
            r = self.client.transaction(OC_VIEWFINDER, [0x00200000, 0, 0])
            if r.ok:
                jpg = extract_viewfinder_jpeg(r.data)
                if jpg:
                    return jpg
            elif r.code not in (RC_NOT_READY, RC_BUSY):
                raise PTPError(f"Canon live view refused (0x{r.code:04X})")
            if time.time() > deadline:
                raise PTPError("The Canon sent no live picture (is the camera in a menu or playing back?).")
            self.poll_events()
            time.sleep(0.05)

    # -- shooting --------------------------------------------------------------------------------------------------
    def capture(self, timeout: float = 20.0, af: bool = True) -> list[tuple[int, bytes]]:
        self.poll_events()
        self.added.clear()
        self._check(self.client.transaction(OC_RELEASE_ON, [3, 0 if af else 1]), "Canon shutter press")
        try:
            time.sleep(0.2)
        finally:
            self._check(self.client.transaction(OC_RELEASE_OFF, [3]), "Canon shutter release")
        deadline, files, last_new = time.time() + timeout, [], 0.0
        while time.time() < deadline:
            self.poll_events()
            while self.added:
                handle, fmt = self.added.pop(0)
                data = self._check(self.client.transaction(OC_GET_OBJECT, [handle]), "Canon GetObject").data
                self.client.transaction(OC_TRANSFER_COMPLETE, [handle])
                files.append((fmt, data))
                last_new = time.time()
            if files and time.time() - last_new > 1.5:          # RAW + JPEG come as two files
                return files
            time.sleep(0.15)
        if files:
            return files
        raise PTPError("The Canon took no picture in time (focus not found, or the card / buffer is full).")

    def movie_record(self, start: bool) -> None:
        if not self.lv_on:
            self._start_live_view()
        self._set_canon(DP_RECORD, 4 if start else 0)
        self.recording = start

    # -- focus -----------------------------------------------------------------------------------------------------
    def focus_step(self, step: int) -> None:
        """Lens drive: negative nearer, positive farther; 1-3 = small, medium, large step (live view only)."""
        if not step:
            return
        if not self.lv_on:
            self._start_live_view()
        size = min(3, max(1, abs(int(step)) // 3 + 1))
        self._check(self.client.transaction(OC_DRIVE_LENS, [size if step < 0 else 0x8000 | size]), "Canon focus")

    def autofocus(self, hold: float = 0.5) -> None:
        self._check(self.client.transaction(OC_DO_AF), "Canon autofocus")
        time.sleep(hold)
        self.client.transaction(OC_AF_CANCEL)

    def set_af_point(self, xf: float, yf: float) -> None:
        # live-view coordinates; Canon bodies use about 6000 x 4000 here (sensor-like units), so scale from fractions
        w, h = 6000, 4000
        self._check(self.client.transaction(OC_AF_POINT, [int(xf * w), int(yf * h)]), "Canon AF point")

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        t0 = time.time()
        if not self.lv_on:
            self._start_live_view()
        self.set_af_point(xf, yf)
        self.autofocus(hold=max_wait)
        return {"acquired": True, "ms_af": int((time.time() - t0) * 1000), "untested": True}

    def capabilities(self) -> dict:
        return {"live_view": self.supports_op(OC_VIEWFINDER), "capture": self.supports_op(OC_RELEASE_ON),
                "movie": True, "focus": self.supports_op(OC_DRIVE_LENS), "zoom": False,
                "settings": sorted(f"0x{c:04X}" for c in self._prop_cache)}


def extract_viewfinder_jpeg(d: bytes) -> bytes | None:
    """GetViewFinderData: records [length u32][type u32][data]; types 1, 9 and 11 hold the JPEG."""
    i = 0
    while i + 8 <= len(d):
        n, kind = struct.unpack_from("<II", d, i)
        if n < 8:
            break
        if kind in (1, 9, 11):
            jpg = d[i + 8:i + n]
            if jpg[:2] == b"\xff\xd8":
                return jpg
        i += n
    s = d.find(b"\xff\xd8")                                    # unknown layout: the first JPEG in the data
    return d[s:] if s >= 0 else None


def canon_code_for_seconds(t: float) -> int:
    """Studio shutter seconds -> Canon code (for tests and fallbacks)."""
    return int(round(0x38 - 8 * math.log2(t))) if t > 0 else 0x0C


__all__ = ["CanonEOSCamera", "CANON_VID", "extract_viewfinder_jpeg", "studio_to_seconds"]
