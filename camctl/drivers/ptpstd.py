"""Standard PTP (ISO 15740) for cameras that are not Sony: the base the Canon and Nikon drivers build on.

UNTESTED ON REAL CAMERAS. Written from the public standard and the codes documented in libgphoto2 (camlibs/ptp2/ptp.h,
LGPL; only the numbers are used, no code is copied). Every function here was tested against simulated cameras only.

How it fits into Studio: Studio was built around a Sony ZV-E10, and its screens read settings by the Sony property
codes (aperture 0x5007 in f x 100, shutter 0xD20D as numerator << 16 | denominator, ISO 0xD21E with 0xFFFFFF = auto,
battery 0xD218 in percent). A driver for another brand translates its camera's own codes and values into those, so
nothing else in Studio needs to know the brand. Only what a driver can really read is shown; what it cannot do
raises Unsupported with a plain sentence.
"""
from __future__ import annotations


import struct
import time

from ..ptpip import PTPError
from ..report import parse_device_info
from .replies import record_ptp
from .sony import PROPS, PropInfo, format_value  # noqa: F401  (format_value: same display rules for every brand)

# -- standard operations -------------------------------------------------------------------------------------------
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_STORAGE_IDS = 0x1004
OC_GET_OBJECT_HANDLES = 0x1007
OC_GET_OBJECT_INFO = 0x1008
OC_GET_OBJECT = 0x1009
OC_INITIATE_CAPTURE = 0x100E
OC_GET_DEVICE_PROP_DESC = 0x1014
OC_GET_DEVICE_PROP_VALUE = 0x1015
OC_SET_DEVICE_PROP_VALUE = 0x1016
RC_OK = 0x2001
RC_DEVICE_BUSY = 0x2019
RC_SESSION_ALREADY_OPEN = 0x201E

# -- standard properties -------------------------------------------------------------------------------------------
DP_BATTERY = 0x5001
DP_WHITE_BALANCE = 0x5005
DP_FNUMBER = 0x5007
DP_FOCUS_MODE = 0x500A
DP_EXPOSURE_TIME = 0x500D        # 1/10000 s units
DP_EXPOSURE_PROGRAM = 0x500E
DP_EXPOSURE_INDEX = 0x500F        # ISO; 0xFFFF = auto on most cameras
DP_EXPOSURE_BIAS = 0x5010         # 1/1000 EV, signed
DP_STILL_CAPTURE_MODE = 0x5013

# Studio's own ids (the Sony codes the screens use)
S_APERTURE = PROPS["aperture"]          # 0x5007, f x 100
S_SHUTTER = PROPS["shutter_speed"]      # 0xD20D, (num << 16) | den, 0 = bulb
S_ISO = PROPS["iso"]                    # 0xD21E, 0xFFFFFF = auto
S_WB = PROPS["white_balance"]           # 0x5005
S_FOCUS_MODE = PROPS["focus_mode"]      # 0x500A
S_EXPOSURE_MODE = PROPS["exposure_mode"]   # 0x500E
S_EV = PROPS["exposure_compensation"]   # 0x5010, 1/1000 EV
S_DRIVE = PROPS["drive_mode"]           # 0x5013
S_BATTERY = 0xD218                      # percent
ISO_AUTO = 0xFFFFFF

DTYPES = {1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 7: "q", 8: "Q"}

# nominal values, to show 1/125 instead of 1/124.99 after a conversion
NOMINAL_DEN = [1, 2, 3, 4, 5, 6, 8, 10, 13, 15, 20, 25, 30, 40, 45, 50, 60, 80, 90, 100, 125, 160, 180, 200, 250, 320,
               350, 400, 500, 640, 750, 800, 1000, 1250, 1500, 1600, 2000, 2500, 3000, 3200, 4000, 5000, 6000, 6400,
               8000, 10000, 12800, 16000, 20000, 25600, 32000]
NOMINAL_SEC = [1, 1.3, 1.5, 1.6, 2, 2.5, 3, 3.2, 4, 5, 6, 8, 10, 13, 15, 20, 25, 30, 60]
NOMINAL_F = [1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 6.7, 7.1,
             8.0, 9.0, 9.5, 10, 11, 13, 14, 16, 18, 19, 20, 22, 25, 27, 29, 32, 36, 40, 45]
NOMINAL_ISO = [50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200, 4000,
               5000, 6400, 8000, 10000, 12800, 16000, 20000, 25600, 32000, 40000, 51200, 64000, 80000, 102400]


class Unsupported(PTPError):
    """This camera (or this driver) cannot do that. The message is a plain sentence for the screen."""


def _nearest(x: float, table) -> float:
    return min(table, key=lambda t: abs(t - x) / t)


# -- value conversions (also used by the Canon and gphoto2 drivers) -------------------------------------------------
def seconds_to_studio(t: float) -> int:
    """Exposure time in seconds -> Studio's shutter code ((num << 16) | den). 0 / negative -> bulb (0)."""
    if t <= 0:
        return 0
    if t >= 0.95:
        s = _nearest(t, NOMINAL_SEC)
        return (int(round(s * 10)) << 16) | 10 if s != int(s) else (int(s) << 16) | 1
    return (1 << 16) | int(_nearest(1 / t, NOMINAL_DEN))


def studio_to_seconds(code: int) -> float:
    if not code:
        return 0.0
    num, den = code >> 16, code & 0xFFFF
    return num / den if den else 0.0


def f_to_studio(f: float) -> int:
    return int(round(_nearest(f, NOMINAL_F) * 100))


def iso_to_studio(iso: float) -> int:
    return int(_nearest(iso, NOMINAL_ISO)) if iso > 0 else ISO_AUTO


# -- PTP datasets ----------------------------------------------------------------------------------------------------
class _R:
    def __init__(self, d: bytes, i: int = 0):
        self.d, self.i = d, i

    def take(self, fmt: str):
        v = struct.unpack_from("<" + fmt, self.d, self.i)
        self.i += struct.calcsize("<" + fmt)
        return v[0] if len(v) == 1 else v

    def value(self, dtype: int):
        if dtype in DTYPES:
            return self.take(DTYPES[dtype])
        if dtype == 0xFFFF:
            n = self.take("B")
            s = self.d[self.i:self.i + 2 * n].decode("utf-16-le", "ignore").rstrip("\x00")
            self.i += 2 * n
            return s
        if dtype & 0x4000 and (dtype & 0xFF) in DTYPES:        # arrays: count + items
            n = self.take("I")
            return [self.take(DTYPES[dtype & 0xFF]) for _ in range(n)]
        raise PTPError(f"unsupported datatype 0x{dtype:x}")


def parse_prop_desc(data: bytes) -> PropInfo:
    """Standard DevicePropDesc dataset -> PropInfo (native code and values)."""
    r = _R(data)
    code, dtype, getset = r.take("HHB")
    r.value(dtype)                     # factory default
    cur = r.value(dtype)
    info = PropInfo(code, dtype, getset == 1, True, cur)
    form = r.take("B") if r.i < len(data) else 0
    if form == 1:
        info.form, info.range = "range", (r.value(dtype), r.value(dtype), r.value(dtype))
    elif form == 2:
        n = r.take("H")
        info.form, info.choices = "enum", [r.value(dtype) for _ in range(n)]
    return info


class PTPCamera:
    """A camera that speaks plain ISO 15740 PTP over USB. Brand drivers subclass it.

    What a plain PTP camera gives: exposure settings that it lists as standard properties, a shutter release
    (InitiateCapture) and the picture files. No live picture, no movie button, no focus or zoom: those are vendor
    extensions and live in the brand drivers.
    """

    kind = "ptp-generic"
    brand = "Camera"
    tested = False
    vendor_id: int | None = None

    # standard property -> (Studio id, to Studio, from Studio)
    STD_MAP = {
        DP_FNUMBER: (S_APERTURE, lambda v: f_to_studio(v / 100), lambda s: s),
        DP_EXPOSURE_TIME: (S_SHUTTER, lambda v: seconds_to_studio(v / 10000) if v not in (0, 0xFFFFFFFF) else 0,
                           lambda s: int(round(studio_to_seconds(s) * 10000)) if s else 0xFFFFFFFF),
        DP_EXPOSURE_INDEX: (S_ISO, lambda v: ISO_AUTO if v in (0, 0xFFFF, 0xFFFFFFFF) else iso_to_studio(v),
                            None),
        DP_EXPOSURE_BIAS: (S_EV, lambda v: v, lambda s: s),
        DP_WHITE_BALANCE: (S_WB, lambda v: v, lambda s: s),
        DP_FOCUS_MODE: (S_FOCUS_MODE, lambda v: v, lambda s: s),
        DP_EXPOSURE_PROGRAM: (S_EXPOSURE_MODE, lambda v: v, lambda s: s),
        DP_STILL_CAPTURE_MODE: (S_DRIVE, lambda v: v, lambda s: s),
        DP_BATTERY: (S_BATTERY, None, None),        # special: converted with its range
    }

    def __init__(self, host: str = "usb", client=None, vendor: int | None = None, port: int | None = None,
                 name: str = "camctl", guid: bytes | None = None):
        if client is not None:
            self.client = client
        elif host == "usb":
            from ..ptpusb import PTPUSBClient
            self.client = PTPUSBClient(vendor=vendor if vendor is not None else self.vendor_id)
        else:                                       # Wi-Fi: standard PTP/IP (Canon EOS, Nikon and others)
            from ..ptpip import PTPIP_PORT, PTPIPClient
            ip, _, p = host.partition(":")
            self.client = PTPIPClient(ip, port or (int(p) if p.isdigit() else PTPIP_PORT), name=name, guid=guid,
                                      timeout=60.0)
        record_ptp(self, self.client)                 # the last operations and answer codes, for the camera report
        self.info: dict = {}
        self._native: dict[int, PropInfo] = {}       # native code -> PropInfo as the camera reports it
        self._prop_cache: dict[int, PropInfo] = {}    # Studio id -> PropInfo in Studio's units
        self._back: dict[int, tuple[int, list]] = {}  # Studio id -> (native code, [(studio value, native value)])

    # -- session ---------------------------------------------------------------------------------------------------
    @property
    def name(self) -> str:
        model = self.info.get("model") or getattr(self.client, "camera_name", "") or ""
        maker = self.brand if self.brand.lower() not in model.lower() else ""
        return f"{maker} {model}".strip() or f"{self.brand} camera"     # the status line adds "untested model"

    def _check(self, resp, what: str):
        if not resp.ok:
            raise PTPError(f"{what} failed with response 0x{resp.code:04X}")
        return resp

    def connect(self, recover: bool = False) -> None:
        try:
            c = self.client
            c.connect()
            if recover and hasattr(c, "ptp_reset"):
                c.ptp_reset()
            resp = c.transaction(OC_OPEN_SESSION, [1])
            if resp.code == RC_SESSION_ALREADY_OPEN:
                c.transaction(OC_CLOSE_SESSION)
                resp = c.transaction(OC_OPEN_SESSION, [1])
            self._check(resp, "OpenSession")
            self.info = self.device_info()
            self._after_open()
            self.refresh()
        except BaseException:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    def _after_open(self) -> None:
        """Brand drivers switch the camera into remote mode here."""

    def close(self) -> None:
        try:
            self._before_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.transaction(OC_CLOSE_SESSION)
        except Exception:  # noqa: BLE001
            pass
        self.client.close()

    def _before_close(self) -> None:
        pass

    def device_info(self) -> dict:
        r = self.client.transaction(OC_GET_DEVICE_INFO)
        if not r.ok:
            raise PTPError(f"DeviceInfo refused (0x{r.code:04X})")
        return parse_device_info(r.data)

    def supports_op(self, code: int) -> bool:
        return f"0x{code:04X}" in self.info.get("operations", [])

    def supports_prop(self, code: int) -> bool:
        return f"0x{code:04X}" in self.info.get("device_properties", [])

    # -- settings --------------------------------------------------------------------------------------------------
    def read_native(self, code: int) -> PropInfo | None:
        r = self.client.transaction(OC_GET_DEVICE_PROP_DESC, [code])
        if not r.ok:
            return None
        info = parse_prop_desc(r.data)
        self._native[code] = info
        return info

    def _translate(self, native: PropInfo) -> PropInfo | None:
        entry = self.STD_MAP.get(native.code)
        if not entry:
            return None
        sid, to_s, _ = entry
        if native.code == DP_BATTERY:
            hi = native.range[1] if native.range else (max(native.choices) if native.choices else 100)
            pct = int(round(100 * native.value / hi)) if hi else -1
            return PropInfo(S_BATTERY, 3, False, True, pct, range=(-1, 100, 1))
        pairs = []
        for v in (native.choices or []):
            try:
                pairs.append((to_s(v), v))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        self._back[sid] = (native.code, pairs)
        dtype = 6 if sid in (S_SHUTTER, S_ISO) else native.datatype
        out = PropInfo(sid, dtype, native.writable, True, to_s(native.value),
                       choices=[s for s, _ in pairs])
        if native.range and not pairs and sid in (S_APERTURE, S_EV, S_WB, S_FOCUS_MODE, S_EXPOSURE_MODE, S_DRIVE):
            out.range = native.range
        return out

    def refresh(self) -> dict[int, PropInfo]:
        for code in self.STD_MAP:
            if self.info and not self.supports_prop(code):
                continue
            try:
                native = self.read_native(code)
            except PTPError:
                continue
            if native is not None:
                p = self._translate(native)
                if p is not None:
                    self._prop_cache[p.code] = p
        return self._prop_cache

    @staticmethod
    def resolve(prop) -> int:
        if isinstance(prop, int):
            return prop
        if prop in PROPS:
            return PROPS[prop]
        return int(prop, 16)

    def _native_value(self, sid: int, value: int):
        """Studio value -> (native code, native value)."""
        code, pairs = self._back.get(sid, (None, []))
        for s, n in pairs:
            if s == value:
                return code, n
        entry = next(((c, e) for c, e in self.STD_MAP.items() if e[0] == sid), None)
        if entry is None:
            raise Unsupported(f"Studio cannot change 0x{sid:04X} on this {self.brand} camera.")
        c, (_, _, from_s) = entry
        if from_s is None:
            if sid == S_ISO:
                return c, 0xFFFF if value == ISO_AUTO else int(value)
            raise Unsupported("This setting can only be read on this camera.")
        return c, from_s(value)

    def set(self, prop, value: int) -> None:
        sid = self.resolve(prop)
        code, nval = self._native_value(sid, int(value))
        native = self._native.get(code) or self.read_native(code)
        if native is None:
            raise PTPError(f"camera does not report property 0x{code:04X}")
        if not native.writable:
            raise PTPError(f"{format_value(sid, value)} cannot be set: the camera keeps this setting itself "
                           "(check the mode dial).")
        fmt = DTYPES.get(native.datatype)
        if fmt is None:
            raise PTPError(f"cannot write datatype 0x{native.datatype:x}")
        self._write_native(code, struct.pack("<" + fmt, nval))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            p = self.refresh().get(sid)
            if p is not None and p.value == value:
                return
            time.sleep(0.1)

    def _write_native(self, code: int, data: bytes) -> None:
        self._check(self.client.transaction(OC_SET_DEVICE_PROP_VALUE, [code], data_out=data), f"set 0x{code:04X}")

    def try_set(self, prop, value: int) -> int:
        try:
            self.set(prop, value)
            return RC_OK
        except PTPError:
            return 0x2002

    def settings(self) -> list[dict]:
        out = []
        for p in self.refresh().values():
            entry = {"name": p.name, "code": f"0x{p.code:04X}", "value": p.value,
                     "display": format_value(p.code, p.value), "writable": p.writable and p.enabled}
            if p.choices:
                entry["choices"] = p.choices
            if p.range:
                entry["range"] = {"min": p.range[0], "max": p.range[1], "step": p.range[2]}
            out.append(entry)
        return out

    # -- pictures --------------------------------------------------------------------------------------------------
    def _handles(self) -> set[int]:
        r = self.client.transaction(OC_GET_OBJECT_HANDLES, [0xFFFFFFFF, 0, 0])
        if not r.ok or len(r.data) < 4:
            return set()
        n = struct.unpack_from("<I", r.data, 0)[0]
        return set(struct.unpack_from(f"<{n}I", r.data, 4))

    def _object(self, handle: int) -> tuple[int, bytes]:
        info = self._check(self.client.transaction(OC_GET_OBJECT_INFO, [handle]), "GetObjectInfo")
        fmt = struct.unpack_from("<H", info.data, 4)[0] if len(info.data) >= 6 else 0x3801
        data = self._check(self.client.transaction(OC_GET_OBJECT, [handle]), "GetObject").data
        return fmt, data

    def capture(self, timeout: float = 20.0, af: bool = True) -> list[tuple[int, bytes]]:
        """Standard shutter release, then the new files (found by comparing the file list before and after)."""
        if not self.supports_op(OC_INITIATE_CAPTURE):
            raise Unsupported(f"This {self.brand} camera does not take pictures on command over PTP.")
        before = self._handles()
        self._check(self.client.transaction(OC_INITIATE_CAPTURE, [0, 0]), "InitiateCapture")
        deadline, files = time.time() + timeout, []
        while time.time() < deadline:
            time.sleep(0.5)
            new = sorted(self._handles() - before)
            if new:
                time.sleep(0.8)                         # RAW + JPEG: the second file lands a moment later
                for h in sorted(self._handles() - before):
                    files.append(self._object(h))
                return files
        raise PTPError("The camera took no picture in time (or kept it on the card only).")

    def fetch_pending(self, timeout: float = 20.0, quiet: float = 1.5, expect=None) -> list:
        return []

    # -- what plain PTP cannot do ----------------------------------------------------------------------------------
    def live_view(self) -> bytes:
        raise Unsupported(f"This {self.brand} camera gives no live picture over plain PTP.")

    def movie_record(self, start: bool) -> None:
        raise Unsupported(f"Studio cannot start or stop video on this {self.brand} camera yet.")

    def zoom(self, direction: int) -> None:
        raise Unsupported("No power zoom control on this camera.")

    def zoom_position(self) -> int | None:
        return None

    def focus_step(self, step: int) -> None:
        raise Unsupported(f"Studio cannot move the focus on this {self.brand} camera yet.")

    def autofocus(self, hold: float = 0.5) -> None:
        raise Unsupported(f"Studio cannot start autofocus on this {self.brand} camera yet.")

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        raise Unsupported(f"Tap to focus is not available on this {self.brand} camera yet.")

    def set_af_point(self, xf: float, yf: float) -> None:
        raise Unsupported(f"Tap to focus is not available on this {self.brand} camera yet.")

    def capabilities(self) -> dict:
        """What this driver can do with this camera, for the guide and the camera check."""
        return {"live_view": False, "capture": self.supports_op(OC_INITIATE_CAPTURE), "movie": False,
                "focus": False, "zoom": False, "settings": sorted(f"0x{c:04X}" for c in self._prop_cache)}
