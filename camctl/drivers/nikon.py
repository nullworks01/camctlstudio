"""Nikon DSLR / Z driver over USB (standard PTP + Nikon's extension). UNTESTED ON A REAL NIKON.

Codes: libgphoto2 camlibs/ptp2 (ptp.h, ptp-pack.c, config.c), checked 14 Nov 2026; only the numbers are used.
Nikon reports most exposure settings as standard PTP properties, so the standard part of Studio's PTP driver reads
them. Nikon additions used here: shutter 0xD100 (numerator << 16 | denominator, the same form Studio uses), live view
0x9201 / 0x9203 / 0x9202, shutter release into the camera's memory for the PC 0x90C0, autofocus 0x90C1, busy check
0x90C8, event list 0x90C7, manual focus drive 0x9204, AF point 0x9205, movie 0x920A / 0x920B.
"""
from __future__ import annotations

import struct
import time

from ..ptpip import PTPError
from .ptpstd import (OC_GET_OBJECT, OC_GET_OBJECT_INFO, S_FOCUS_MODE, S_SHUTTER, PTPCamera, Unsupported,
                     DP_EXPOSURE_TIME, DP_FOCUS_MODE, seconds_to_studio, studio_to_seconds)

NIKON_VID = 0x04B0

OC_CAPTURE_SDRAM = 0x90C0
OC_AF_DRIVE = 0x90C1
OC_GET_EVENT = 0x90C7
OC_DEVICE_READY = 0x90C8
OC_LV_START = 0x9201
OC_LV_END = 0x9202
OC_LV_IMAGE = 0x9203
OC_MF_DRIVE = 0x9204
OC_AF_AREA = 0x9205
OC_MOVIE_START = 0x920A
OC_MOVIE_END = 0x920B

DP_NIKON_SHUTTER = 0xD100
EC_OBJECT_ADDED = 0x4002
EC_ADDED_IN_SDRAM = 0xC101
EC_CAPTURE_COMPLETE_SDRAM = 0xC102
RC_BUSY = 0x2019
RC_NIKON_NOT_LIVE_VIEW = 0xA00B

FOCUS_TO_STUDIO = {1: 1, 2: 2, 0x8010: 2, 0x8011: 32772, 0x8012: 32773, 0x8013: 32772}
FOCUS_FROM_STUDIO = {1: 1, 2: 0x8010, 32772: 0x8011, 32773: 0x8012}


def _nikon_shutter(v: int) -> int:
    """0xD100 is already numerator << 16 | denominator; 0xFFFFFFFF = bulb, 0xFFFFFFFE = x-sync / time."""
    if v in (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD) or not v:
        return 0
    num, den = v >> 16, v & 0xFFFF
    return seconds_to_studio(num / den) if den else 0


class NikonCamera(PTPCamera):
    kind = "nikon"
    brand = "Nikon"
    vendor_id = NIKON_VID

    STD_MAP = dict(PTPCamera.STD_MAP)
    STD_MAP[DP_FOCUS_MODE] = (S_FOCUS_MODE, lambda v: FOCUS_TO_STUDIO.get(v, v), lambda s: FOCUS_FROM_STUDIO.get(s, s))
    STD_MAP[DP_NIKON_SHUTTER] = (S_SHUTTER, _nikon_shutter,
                                 lambda s: s if s else 0xFFFFFFFF)

    def __init__(self, host: str = "usb", client=None, vendor: int | None = None, **wifi):
        super().__init__(host, client, vendor, **wifi)
        self.lv_on = False
        self.lv_size = (640, 424)

    def read_native(self, code):
        # Nikon's own shutter property wins over the standard one (finer, and it knows bulb)
        if code == DP_EXPOSURE_TIME and self.supports_prop(DP_NIKON_SHUTTER):
            return None
        return super().read_native(code)

    # -- helpers ---------------------------------------------------------------------------------------------------
    def wait_ready(self, timeout: float = 5.0) -> None:
        if not self.supports_op(OC_DEVICE_READY):
            time.sleep(0.3)
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.transaction(OC_DEVICE_READY)
            if r.code != RC_BUSY:
                return
            time.sleep(0.05)
        raise PTPError("The Nikon stayed busy.")

    def events(self) -> list[tuple[int, int]]:
        if not self.supports_op(OC_GET_EVENT):
            return []
        r = self.client.transaction(OC_GET_EVENT)
        if not r.ok or len(r.data) < 2:
            return []
        n = struct.unpack_from("<H", r.data, 0)[0]
        n = min(n, (len(r.data) - 2) // 6)
        return [struct.unpack_from("<HI", r.data, 2 + 6 * i) for i in range(n)]

    # -- live view -------------------------------------------------------------------------------------------------
    def live_view(self) -> bytes:
        if not self.supports_op(OC_LV_IMAGE):
            raise Unsupported("This Nikon gives no live picture over USB.")
        if not self.lv_on:
            r = self.client.transaction(OC_LV_START)
            if not r.ok and r.code != RC_BUSY:
                raise PTPError(f"Nikon live view could not start (0x{r.code:04X}); is the camera in a menu?")
            self.wait_ready()
            self.lv_on = True
        for _ in range(20):
            r = self.client.transaction(OC_LV_IMAGE)
            if r.ok:
                s = r.data.find(b"\xff\xd8")
                if s >= 0:
                    jpg = r.data[s:]
                    self.lv_size = _jpeg_size(jpg) or self.lv_size
                    return jpg
            elif r.code not in (RC_BUSY, RC_NIKON_NOT_LIVE_VIEW):
                raise PTPError(f"Nikon live view refused (0x{r.code:04X})")
            time.sleep(0.05)
        raise PTPError("The Nikon sent no live picture.")

    def _before_close(self) -> None:
        if self.lv_on:
            self.client.transaction(OC_LV_END)

    # -- shooting --------------------------------------------------------------------------------------------------
    def capture(self, timeout: float = 20.0, af: bool = True) -> list[tuple[int, bytes]]:
        if not self.supports_op(OC_CAPTURE_SDRAM):
            return super().capture(timeout, af)
        self.events()                                              # drop old events
        r = self.client.transaction(OC_CAPTURE_SDRAM, [0xFFFFFFFF if af else 0xFFFFFFFE])
        self._check(r, "Nikon shutter")
        deadline, handles, done = time.time() + timeout, [], False
        while time.time() < deadline and not (done and handles):
            time.sleep(0.2)
            for code, p in self.events():
                if code in (EC_OBJECT_ADDED, EC_ADDED_IN_SDRAM) and p not in handles:
                    handles.append(p)
                elif code == EC_CAPTURE_COMPLETE_SDRAM:
                    done = True
            if handles and not done and not self.supports_op(OC_GET_EVENT):
                done = True
        if not handles:
            raise PTPError("The Nikon took no picture in time (focus not found, or the buffer is full).")
        files = []
        for h in handles:
            info = self._check(self.client.transaction(OC_GET_OBJECT_INFO, [h]), "Nikon GetObjectInfo")
            fmt = struct.unpack_from("<H", info.data, 4)[0] if len(info.data) >= 6 else 0x3801
            files.append((fmt, self._check(self.client.transaction(OC_GET_OBJECT, [h]), "Nikon GetObject").data))
        return files

    def movie_record(self, start: bool) -> None:
        if not self.supports_op(OC_MOVIE_START):
            raise Unsupported("This Nikon does not start video over USB.")
        if start and not self.lv_on:
            self.live_view()
        self._check(self.client.transaction(OC_MOVIE_START if start else OC_MOVIE_END), "Nikon movie")

    # -- focus -----------------------------------------------------------------------------------------------------
    def focus_step(self, step: int) -> None:
        if not step:
            return
        if not self.supports_op(OC_MF_DRIVE):
            raise Unsupported("This Nikon does not move the focus over USB.")
        if not self.lv_on:
            self.live_view()
        self._check(self.client.transaction(OC_MF_DRIVE, [1 if step < 0 else 2, abs(int(step)) * 100]), "Nikon focus")
        self.wait_ready()

    def autofocus(self, hold: float = 0.5) -> None:
        self._check(self.client.transaction(OC_AF_DRIVE), "Nikon autofocus")
        self.wait_ready(timeout=max(hold, 3.0))

    def set_af_point(self, xf: float, yf: float) -> None:
        if not self.lv_on:
            self.live_view()
        w, h = self.lv_size
        self._check(self.client.transaction(OC_AF_AREA, [int(xf * w), int(yf * h)]), "Nikon AF point")

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        t0 = time.time()
        self.set_af_point(xf, yf)
        self.autofocus(hold=max_wait)
        return {"acquired": True, "ms_af": int((time.time() - t0) * 1000), "untested": True}

    def capabilities(self) -> dict:
        return {"live_view": self.supports_op(OC_LV_IMAGE), "capture": self.supports_op(OC_CAPTURE_SDRAM)
                or self.supports_op(0x100E), "movie": self.supports_op(OC_MOVIE_START),
                "focus": self.supports_op(OC_MF_DRIVE), "zoom": False,
                "settings": sorted(f"0x{c:04X}" for c in self._prop_cache)}


def _jpeg_size(jpg: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(jpg):
        if jpg[i] != 0xFF:
            return None
        marker, seg = jpg[i + 1], struct.unpack_from(">H", jpg, i + 2)[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack_from(">HH", jpg, i + 5)
            return w, h
        i += 2 + seg
    return None


__all__ = ["NikonCamera", "NIKON_VID", "studio_to_seconds"]
