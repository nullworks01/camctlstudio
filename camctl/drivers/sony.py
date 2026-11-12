"""Sony Alpha / ZV driver over Wi-Fi (PTP/IP with Sony 'SDIO' extensions).

Based on the operation and property codes documented in libgphoto2 (ptp.h)
and used by the Alpha Fairy project. UNTESTED against real hardware yet.

Camera setup (ZV-E10): Menu > Network > Ctrl w/ Smartphone > On, then
'Connection'. The camera shows a Wi-Fi network (DIRECT-xxxx) and password.
Join it from the bridge computer; the camera is normally at 192.168.122.1.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from ..ptpip import PTPError, PTPIPClient

DEFAULT_HOST = "192.168.122.1"

# Standard PTP operations
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_OBJECT_INFO = 0x1008
OC_GET_OBJECT = 0x1009

# Sony vendor operations
OC_SDIO_CONNECT = 0x9201
OC_SDIO_GET_EXT_DEVICE_INFO = 0x9202
OC_SDIO_SET_EXT_DEVICE_PROP_VALUE = 0x9205
OC_SDIO_CONTROL_DEVICE = 0x9207
OC_SDIO_GET_ALL_EXT_DEVICE_PROP_INFO = 0x9209

RC_ACCESS_DENIED = 0x200F
RC_SESSION_ALREADY_OPEN = 0x201E

FMT_JPEG = 0x3801
FMT_EXT = {FMT_JPEG: ".jpg", 0xB101: ".arw", 0xB301: ".heif"}

HANDLE_CAPTURED_IMAGE = 0xFFFFC001
S1_SETTLE_S = 3.2        # half press held before the shutter; 1.5 s was not enough (11 Nov 2026), see capture

# capture diagnostics: commands and watched properties around a shot, kept in memory and in a log file
CAPTURE_WATCH = (0xD215, 0xD222, 0xD249, 0xD24A, 0xD248, 0xD20E, 0xD213, 0xD217, 0xD21D)
CAPTURE_LOG: list[str] = []


def _clog(event: str, **kw) -> None:
    import os
    from pathlib import Path as _P
    line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {event} " + \
        " ".join(f"{k}={v}" for k, v in kw.items())
    CAPTURE_LOG.append(line)
    del CAPTURE_LOG[:-400]
    try:
        f = _P(os.environ.get("APPDATA") or _P.home()) / "camctl" / "logs" / "capture.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
HANDLE_LIVE_VIEW = 0xFFFFC002

# Controls (sent with ControlDevice; value 2 = press, 1 = release)
CTRL_HALF_PRESS = 0xD2C1   # S1: autofocus
CTRL_FULL_PRESS = 0xD2C2   # S2: shutter
CTRL_MOVIE_REC = 0xD2C8
CTRL_NEAR_FAR = 0xD2D1          # manual focus step, int16 -7..+7
CTRL_AF_AREA_POSITION = 0xD2DC  # AF point, uint32 (x << 16) | y in a 640x480 grid
CTRL_ZOOM = 0xD2DD               # power zoom: int16 +1 closer, -1 wider, 0 stop (one fixed speed; 2..8 are ignored)
PROP_ZOOM_POSITION = 0xD25D      # low byte 0 (wide) .. 100 (tele); high byte 1 photo / 2 movie (verified 12 Nov 2026)
# zoom position -> focal length, measured on his E PZ 16-50 by the picture's magnification against the wide end
# (two runs agree within 0.4 mm; 100 = 50 mm from the lens' spec, the match was lost beyond 80)
ZOOM_MM = [(0, 16.0), (16, 18.4), (24, 21.5), (52, 31.9), (67, 36.6), (80, 42.5), (100, 50.0)]


def zoom_mm(pos: int) -> float:
    pos = max(0, min(100, int(pos)))
    for (p0, m0), (p1, m1) in zip(ZOOM_MM, ZOOM_MM[1:]):
        if pos <= p1:
            return m0 + (m1 - m0) * (pos - p0) / (p1 - p0)
    return ZOOM_MM[-1][1]


def zoom_pos_for(mm: float) -> int:
    mm = max(ZOOM_MM[0][1], min(ZOOM_MM[-1][1], float(mm)))
    for (p0, m0), (p1, m1) in zip(ZOOM_MM, ZOOM_MM[1:]):
        if mm <= m1:
            return int(round(p0 + (p1 - p0) * (mm - m0) / (m1 - m0)))
    return 100
PRESS, RELEASE = 2, 1

PROP_FOCUS_AREA = 0xD22C        # 1 Wide, 2 Zone, 3 Center, 257/258/259 Flexible Spot S/M/L
FLEXIBLE_SPOTS = (257, 258, 259, 260)

# Properties with friendly names. Anything else is still reachable by hex code.
PROPS = {
    "white_balance": 0x5005,
    "aperture": 0x5007,          # f-number * 100 (280 = f/2.8)
    "focus_mode": 0x500A,
    "exposure_mode": 0x500E,
    "exposure_compensation": 0x5010,  # 1/1000 EV, signed
    "drive_mode": 0x5013,
    "shutter_speed": 0xD20D,     # (numerator << 16) | denominator; 0 = bulb
    "objects_in_memory": 0xD215,
    "iso": 0xD21E,               # 0xFFFFFF = auto
}
PROP_NAMES = {v: k for k, v in PROPS.items()}

# PTP datatype code -> struct format
DTYPES = {1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 7: "q", 8: "Q"}


@dataclass
class PropInfo:
    code: int
    datatype: int
    writable: bool
    enabled: bool
    value: object
    form: str = "none"
    choices: list = field(default_factory=list)   # every value the camera knows
    settable: list = field(default_factory=list)  # values settable right now (may be empty)
    range: tuple | None = None

    @property
    def name(self) -> str:
        return PROP_NAMES.get(self.code, f"0x{self.code:04X}")


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        v = struct.unpack_from("<" + fmt, self.d, self.i)
        self.i += size
        return v[0] if len(v) == 1 else v

    def value(self, dtype: int):
        if dtype in DTYPES:
            return self.take(DTYPES[dtype])
        if dtype == 0xFFFF:  # PTP string: u8 char count, UTF-16LE
            n = self.take("B")
            s = self.d[self.i:self.i + n * 2].decode("utf-16-le", "ignore").rstrip("\x00")
            self.i += n * 2
            return s
        raise PTPError(f"unsupported datatype 0x{dtype:x}")


def parse_all_props(data: bytes) -> dict[int, PropInfo]:
    r = _Reader(data)
    count = r.take("Q")
    props: dict[int, PropInfo] = {}
    for _ in range(count):
        code, dtype, getset, enabled = r.take("HHBB")
        r.value(dtype)  # factory default
        cur = r.value(dtype)
        info = PropInfo(code, dtype, getset == 1, enabled == 1, cur)
        form = r.take("B")
        if form == 1:
            info.form = "range"
            info.range = (r.value(dtype), r.value(dtype), r.value(dtype))
        elif form == 2:
            info.form = "enum"
            n = r.take("H")
            info.choices = [r.value(dtype) for _ in range(n)]
            # Newer Sony protocol versions append a second list: the values
            # settable right now. It can shrink to just the current value while
            # the camera is busy, so keep it separately and never let it
            # replace the full list.
            if r.i + 2 <= len(r.d):
                save = r.i
                try:
                    n2 = r.take("H")
                    info.settable = [r.value(dtype) for _ in range(n2)]
                except (struct.error, PTPError):
                    r.i = save
        props[code] = info
    return props


def format_value(code: int, v) -> str:
    if code == PROPS["aperture"] and isinstance(v, int):
        return f"f/{v / 100:g}"
    if code == PROPS["iso"] and isinstance(v, int):
        return "auto" if v == 0xFFFFFF else str(v)
    if code == PROPS["shutter_speed"] and isinstance(v, int):
        if v == 0:
            return "bulb"
        num, den = v >> 16, v & 0xFFFF
        return f"{num}/{den}s" if den != 1 else f"{num}s"
    if code == PROPS["exposure_compensation"] and isinstance(v, int):
        return f"{v / 1000:+g} EV"
    return str(v)


class SonyCamera:
    """High-level control of a Sony camera in smartphone/remote mode."""

    kind = "sony-ptpip"

    def __init__(self, host: str = DEFAULT_HOST, port: int = 15740, guid: bytes | None = None,
                 name: str = "camctl", timeout: float = 10.0):
        """host: camera IP for Wi-Fi, or "usb" for a USB cable (PC Remote mode).
        guid: keep it the same between runs over Wi-Fi, the camera pairs with it."""
        if host.lower() == "usb":
            from ..ptpusb import PTPUSBClient
            self.client = PTPUSBClient()
        else:
            self.client = PTPIPClient(host, port, name=name, guid=guid, timeout=timeout)
        self._prop_cache: dict[int, PropInfo] = {}

    @property
    def name(self) -> str:
        return self.client.camera_name or "Sony camera"

    def connect(self, recover: bool = False) -> None:
        """Open the session. recover=True first resets a camera left stuck mid-transfer.
        On any failure the transport is released again (no leaked USB claim)."""
        try:
            self._connect(recover)
        except BaseException:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    def _connect(self, recover: bool) -> None:
        c = self.client
        c.connect()
        if recover and hasattr(c, "ptp_reset"):
            c.ptp_reset()
        self._prop_cache = {}
        resp = c.transaction(OC_OPEN_SESSION, [1])
        if resp.code == RC_SESSION_ALREADY_OPEN:
            # Left open by an earlier run that did not close cleanly.
            c.transaction(OC_CLOSE_SESSION)
            resp = c.transaction(OC_OPEN_SESSION, [1])
        self._check(resp, "OpenSession")
        # Sony handshake: phase 1, 2, version query, phase 3.
        self._check(c.transaction(OC_SDIO_CONNECT, [1, 0, 0]), "SDIO_Connect 1")
        self._check(c.transaction(OC_SDIO_CONNECT, [2, 0, 0]), "SDIO_Connect 2")
        for version in (0x012C, 0x00C8):  # protocol 3.00, then 2.00
            if c.transaction(OC_SDIO_GET_EXT_DEVICE_INFO, [version]).ok:
                break
        self._check(c.transaction(OC_SDIO_CONNECT, [3, 0, 0]), "SDIO_Connect 3")
        self.refresh()

    def close(self) -> None:
        """End the PTP session cleanly so the camera is ready for the next run."""
        old = getattr(self.client, "timeout", None) if hasattr(self.client, "ptp_reset") else None   # USB: ms
        try:
            if old is not None:
                self.client.timeout = 1500       # don't hang on exit if the camera is gone
            self.client.transaction(OC_CLOSE_SESSION)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if old is not None:
                self.client.timeout = old
        self.client.close()

    @staticmethod
    def _check(resp, what: str):
        if not resp.ok:
            raise PTPError(f"{what} failed with response 0x{resp.code:04X}")
        return resp

    # -- settings ---------------------------------------------------------
    def refresh(self) -> dict[int, PropInfo]:
        # No parameter = full list. (Parameter 1 asks for changed properties only,
        # which comes back empty when nothing changed.) Merge so a short reply
        # never wipes properties we already know.
        resp = self._check(self.client.transaction(OC_SDIO_GET_ALL_EXT_DEVICE_PROP_INFO),
                           "GetAllExtDevicePropInfo")
        self._prop_cache.update(parse_all_props(resp.data))
        return self._prop_cache

    def settings(self) -> list[dict]:
        out = []
        for p in self.refresh().values():
            entry = {"name": p.name, "code": f"0x{p.code:04X}", "value": p.value,
                     "display": format_value(p.code, p.value), "writable": p.writable and p.enabled}
            if p.choices:
                entry["choices"] = p.choices
            if p.settable and set(p.settable) != set(p.choices):
                entry["settable_now"] = p.settable
            if p.range:
                entry["range"] = {"min": p.range[0], "max": p.range[1], "step": p.range[2]}
            out.append(entry)
        return out

    @staticmethod
    def resolve(prop: str | int) -> int:
        if isinstance(prop, int):
            return prop
        if prop in PROPS:
            return PROPS[prop]
        return int(prop, 16)

    def set(self, prop: str | int, value: int) -> None:
        code = self.resolve(prop)
        info = self._prop_cache.get(code) or self.refresh().get(code)
        if info is not None and not (info.writable and info.enabled):
            # right after a half press the camera keeps some properties (focus mode) locked for a moment:
            # wait for it rather than failing (verified on the ZV-E10 over USB, 11 Nov 2026)
            deadline = time.time() + 2.0
            while True:
                info = self.refresh().get(code)
                if info is None or (info.writable and info.enabled) or time.time() >= deadline:
                    break
                time.sleep(0.15)
        if info is None:
            raise PTPError(f"camera does not report property 0x{code:04X}")
        if not (info.writable and info.enabled):
            raise PTPError(f"{info.name} is not changeable in the current mode")
        fmt = DTYPES.get(info.datatype)
        if fmt is None:
            raise PTPError(f"cannot write datatype 0x{info.datatype:x}")
        self._check(self.client.transaction(OC_SDIO_SET_EXT_DEVICE_PROP_VALUE, [code],
                                            data_out=struct.pack("<" + fmt, value)),
                    f"set {info.name}")
        # The camera applies changes asynchronously (~1 s). Wait until it reports
        # the new value so callers reading it back see the change.
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.1)                                    # check often: a tap should not wait longer than needed
            cur = self.refresh().get(code)
            if cur is not None and cur.value == value:
                return

    def _control(self, code: int, value: int) -> None:
        resp = self.client.transaction(OC_SDIO_CONTROL_DEVICE, [code], data_out=struct.pack("<H", value))
        if code in (CTRL_HALF_PRESS, CTRL_FULL_PRESS, CTRL_MOVIE_REC):
            _clog(f"control 0x{code:04X}={value}", rc=hex(resp.code))
        self._check(resp, f"control 0x{code:04X}")

    # -- actions ----------------------------------------------------------
    def live_view(self) -> bytes:
        # Right after connecting the camera answers Access Denied (0x200F) until
        # live view is ready, typically for 1-2 s. It also answers Access Denied
        # whenever the next frame isn't ready yet, so retry quickly: a 0.3 s
        # sleep here capped the live view at about 3.5 fps (20 ms gives about 17).
        deadline = time.time() + 3.0
        while True:
            resp = self.client.transaction(OC_GET_OBJECT, [HANDLE_LIVE_VIEW])
            if resp.code != RC_ACCESS_DENIED or time.time() >= deadline:
                break
            time.sleep(0.02)
        self._check(resp, "live view")
        return extract_jpeg(resp.data)

    # -- focus ------------------------------------------------------------
    def set_af_point(self, xf: float, yf: float) -> None:
        """Move the AF point (Flexible Spot) to a position given as 0..1 of the frame."""
        x = int(min(max(xf, 0.0), 1.0) * 639)
        y = int(min(max(yf, 0.0), 1.0) * 479)
        self._check(self.client.transaction(OC_SDIO_CONTROL_DEVICE, [CTRL_AF_AREA_POSITION],
                                            data_out=struct.pack("<I", (x << 16) | y)), "AF point")

    def try_set(self, prop: str | int, value: int) -> int:
        """Send a value without waiting or checking first; returns the camera's response code (0x2001 = OK)."""
        code = self.resolve(prop)
        info = self._prop_cache.get(code) or self.refresh().get(code)
        fmt = DTYPES.get(info.datatype) if info is not None else None
        if fmt is None:
            return 0
        return self.client.transaction(OC_SDIO_SET_EXT_DEVICE_PROP_VALUE, [code], data_out=struct.pack("<" + fmt, value)).code

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        """Tap-to-focus: Flexible Spot at (xf, yf), then hold the half press until the camera reports focus.

        Verified 11 Nov 2026: the old version read the focus mode from a cached property list taken before the
        caller switched to AF-S, saw "MF" and skipped the half press, so a tap never focused at all. Now the mode is
        read fresh, and the half press is held until the focus indication (0xD213) changes or max_wait passes.
        Returns timings and the indication values seen, so the caller can tell found from not found."""
        t0 = time.time()
        props = self.refresh()
        area = props.get(PROP_FOCUS_AREA)
        if area is not None and area.value not in FLEXIBLE_SPOTS:
            spot = next((v for v in (259, 258, 257) if v in area.choices), None)
            if spot is not None:
                self.set(PROP_FOCUS_AREA, spot)
        self.set_af_point(xf, yf)
        mode = self.refresh().get(PROPS["focus_mode"])            # fresh: the caller may have just changed it
        out = {"acquired": False, "indication": [], "ms_setup": int((time.time() - t0) * 1000)}
        if mode is None or mode.value == 1:
            out["why"] = "manual focus"
            return out
        _clog("AF start", x=round(xf, 3), y=round(yf, 3), mode=mode.value)
        t1 = time.time()
        self._control(CTRL_HALF_PRESS, PRESS)
        first = None
        try:
            while time.time() - t1 < max_wait:
                time.sleep(0.15)
                ind = getattr(self.refresh().get(0xD213), "value", None)
                out["indication"].append(ind)
                first = ind if first is None else first
                if time.time() - t1 >= 0.5 and ind != first and len(out["indication"]) >= 2 \
                        and out["indication"][-2] == ind:           # changed and stayed: the camera decided
                    break
            if lock_in_s1:                                    # lock while the half press still holds the focus
                out["rc_lock_in_s1"] = hex(self.try_set(PROPS["focus_mode"], 1))
        finally:
            self._control(CTRL_HALF_PRESS, RELEASE)
        out["acquired"] = True
        out["ms_af"] = int((time.time() - t1) * 1000)
        _clog("AF end", ms=out["ms_af"], indication=out["indication"])
        return out

    def device_info(self) -> dict:
        """The standard PTP DeviceInfo the camera announces (for the camera report; read-only)."""
        from ..report import parse_device_info
        r = self.client.transaction(OC_GET_DEVICE_INFO)
        if not r.ok:
            raise PTPError(f"DeviceInfo refused (0x{r.code:04X})")
        return parse_device_info(r.data)

    def focus_step(self, step: int) -> None:
        """Manual focus pull: negative = nearer, positive = farther (-7..7 per call)."""
        step = int(min(max(step, -7), 7))
        self._check(self.client.transaction(OC_SDIO_CONTROL_DEVICE, [CTRL_NEAR_FAR],
                                            data_out=struct.pack("<h", step)), "focus step")

    def zoom(self, direction: int) -> None:
        """Power zoom: +1 closer, -1 wider, 0 stop. Lens movement only."""
        d = 1 if direction > 0 else -1 if direction < 0 else 0
        self._check(self.client.transaction(OC_SDIO_CONTROL_DEVICE, [CTRL_ZOOM], data_out=struct.pack("<h", d)), "zoom")

    def zoom_position(self) -> int | None:
        """0 (wide) .. 100 (tele), or None when the camera reports no power zoom."""
        p = self.refresh().get(PROP_ZOOM_POSITION)
        if p is None or not isinstance(p.value, int):
            return None
        return p.value & 0xFF

    def autofocus(self, hold: float = 0.5) -> None:
        self._control(CTRL_HALF_PRESS, PRESS)
        time.sleep(hold)
        self._control(CTRL_HALF_PRESS, RELEASE)

    def _pending_objects(self) -> int:
        props = self.refresh()
        info = props.get(PROPS["objects_in_memory"])
        v = info.value if info and isinstance(info.value, int) else 0
        _clog("poll", **{f"0x{c:04X}": getattr(props.get(c), "value", None) for c in CAPTURE_WATCH})
        return v & 0x7FFF if v & 0x8000 else 0

    def fetch_pending(self, timeout: float = 20.0, quiet: float = 1.5, expect: set | None = None) -> list[tuple[int, bytes]]:
        """Pull every image the camera has queued for transfer.

        Returns (object_format, data) pairs, e.g. (0x3801, jpeg) and (0xB101, arw).
        Keeps going until the queue has been empty for `quiet` seconds after at
        least one file arrived, or `timeout` runs out.
        """
        files: list[tuple[int, bytes]] = []
        deadline = time.time() + timeout
        empty_since = None
        while time.time() < deadline:
            if self._pending_objects():
                empty_since = None
                info = self.client.transaction(OC_GET_OBJECT_INFO, [HANDLE_CAPTURED_IMAGE])
                _clog("GetObjectInfo 0xFFFFC001", rc=hex(info.code), n=len(info.data))
                fmt = struct.unpack_from("<H", info.data, 4)[0] if info.ok and len(info.data) >= 6 else 0
                resp = self.client.transaction(OC_GET_OBJECT, [HANDLE_CAPTURED_IMAGE])
                _clog("GetObject 0xFFFFC001", rc=hex(resp.code), n=len(resp.data))
                if resp.ok and resp.data:
                    if not fmt:
                        fmt = FMT_JPEG if resp.data[:2] == b"\xff\xd8" else 0
                    files.append((fmt, resp.data))
                else:
                    time.sleep(0.3)  # not ready yet; don't hammer the camera
                continue
            if files:
                if expect and expect <= {f for f, _ in files}:
                    break                          # the photo's files are all here (RAW + JPEG): no idle wait
                empty_since = empty_since or time.time()
                if time.time() - empty_since >= quiet:
                    break
            time.sleep(0.3)
        return files

    def capture(self, timeout: float = 20.0, af: bool = True) -> list[tuple[int, bytes]]:
        """Autofocus, take a photo, and return every file the camera transfers
        (JPEG and/or RAW) as (object_format, data) pairs. Empty if none arrived;
        the photo is then only on the memory card (or nowhere, if the camera is
        set to save to PC only)."""
        # Anything still queued from an earlier shot is collected by fetch_pending
        # below together with the new files, so nothing is dropped.
        # Sony PC Remote (ZV-E10, protocol 3.00, verified on the real camera 11 Nov 2026): the camera
        # ignores S2 unless S1 (half press) has been held long enough first. S2 sent 0.3-0.4 s after S1 was
        # answered "OK" but never released (shot counter unchanged, three times); S1 held 3.2 s released
        # in MF and in AF-S. In MF (Studio's focus lock) S1 does not refocus, so it is always sent.
        props = self.refresh()
        _clog("before", **{f"0x{c:04X}": getattr(props.get(c), "value", None) for c in CAPTURE_WATCH})
        self._control(CTRL_HALF_PRESS, PRESS)
        _clog("S1 down")
        time.sleep(S1_SETTLE_S)
        self._control(CTRL_FULL_PRESS, PRESS)
        _clog("S2 down")
        time.sleep(0.4)
        self._control(CTRL_FULL_PRESS, RELEASE)
        _clog("S2 up")
        self._control(CTRL_HALF_PRESS, RELEASE)
        _clog("S1 up")
        sf = getattr(self.refresh().get(0xD253), "value", None)         # 1 RAW, 2 RAW + JPEG, 3 JPEG
        expect = {1: {0xB101}, 2: {0xB101, FMT_JPEG}, 3: {FMT_JPEG}}.get(sf)
        files = self.fetch_pending(timeout=timeout, expect=expect)
        props = self.refresh()
        _clog("after", files=[(hex(f), len(d)) for f, d in files],
              **{f"0x{c:04X}": getattr(props.get(c), "value", None) for c in CAPTURE_WATCH})
        return files

    def movie_record(self, start: bool) -> None:
        # Movie Rec is a toggle on most bodies: press then release.
        self._control(CTRL_MOVIE_REC, PRESS)
        time.sleep(0.2)
        self._control(CTRL_MOVIE_REC, RELEASE)


def extract_jpeg(data: bytes) -> bytes:
    """Sony live-view objects carry a small header with the JPEG offset and size."""
    if len(data) >= 8:
        offset, size = struct.unpack_from("<II", data, 0)
        if 8 <= offset < len(data) and size and offset + size <= len(data):
            jpg = data[offset:offset + size]
            if jpg[:2] == b"\xff\xd8":
                # The reported size can include padding after the end marker.
                end = jpg.rfind(b"\xff\xd9")
                return jpg[:end + 2] if end > 0 else jpg
    start = data.find(b"\xff\xd8")
    end = data.rfind(b"\xff\xd9")
    if start < 0:
        raise PTPError("no JPEG in live view data")
    return data[start:end + 2 if end > start else len(data)]

# -- Sony property and control codes Studio uses, in one place (the rest of Studio uses these names) -------------
SP_FLASH_COMP = 0xD200
SP_DRO = 0xD201
SP_IMAGE_SIZE = 0xD203
SP_SHUTTER = 0xD20D
SP_STATUS = 0xD20E
SP_COLOR_TEMP = 0xD20F
SP_WB_SHIFT_GM = 0xD210
SP_ASPECT = 0xD211
SP_FOCUS_INDICATION = 0xD213
SP_PENDING_IMAGES = 0xD215
SP_AF_LOCK = 0xD217
SP_BATTERY = 0xD218
SP_PICTURE_EFFECT = 0xD21B
SP_WB_SHIFT_AB = 0xD21C
SP_REC_STATE = 0xD21D
SP_ISO = 0xD21E
SP_SAVE_TO = 0xD222
SP_FOCUS_AREA = 0xD22C
SP_PICTURE_PROFILE = 0xD23F
SP_MOVIE_FORMAT = 0xD241
SP_MOVIE_SETTING = 0xD242
SP_SHOOT_CLASS = 0xD247
SP_MEDIA_STATUS = 0xD248
SP_SHOTS_LEFT = 0xD249
SP_REC_TIME_LEFT = 0xD24A
SP_FOCUS_POSITION = 0xD24C
SP_STILL_FORMAT = 0xD253
SP_ZOOM_POSITION = 0xD25D
SP_JPEG_QUALITY = 0xD25F
SP_LIVE_VIEW_QUALITY = 0xD26A
SC_STILL_D2C7 = 0xD2C7
