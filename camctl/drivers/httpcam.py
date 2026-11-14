"""Shared parts of the Wi-Fi drivers for cameras that speak HTTP instead of PTP (Panasonic LUMIX, OM System /
Olympus, Ricoh / Pentax). UNTESTED ON REAL CAMERAS: written from public documentation and open-source clients
(see docs/cameras.md, "Where the codes come from") and tested against simulated cameras only.

These cameras name their settings with strings ("1/250", "5.6", "auto"). A driver lists, for each Studio setting,
the camera's key and how to turn one of its strings into Studio's units; this module turns that into the same
PropInfo objects the PTP drivers give, so nothing else in Studio needs to know the brand.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..ptpip import PTPError
from .ptpstd import (ISO_AUTO, S_APERTURE, S_BATTERY, S_DRIVE, S_EV, S_EXPOSURE_MODE, S_FOCUS_MODE, S_ISO,
                     S_SHUTTER, S_WB, Unsupported, f_to_studio, iso_to_studio, seconds_to_studio)
from .sony import PROPS, PropInfo, format_value

__all__ = ["HTTPCamera", "FrameStream", "Unsupported", "S_APERTURE", "S_SHUTTER", "S_ISO", "S_EV", "S_WB",
           "S_FOCUS_MODE", "S_EXPOSURE_MODE", "S_DRIVE", "S_BATTERY", "ISO_AUTO", "num", "wb_from_name",
           "iso_from_str", "f_from_str", "ev_from_str", "jpeg_between"]


# -- value helpers ---------------------------------------------------------------------------------------------------
def num(s) -> float | None:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(s))
    return float(m.group(0)) if m else None


def iso_from_str(s: str) -> int:
    if str(s).strip().lower() in ("auto", "iso auto", "a", "auto iso", ""):
        return ISO_AUTO
    v = num(s)
    if v is None:
        raise ValueError(s)
    return iso_to_studio(v)


def f_from_str(s: str) -> int:
    v = num(s)
    if v is None or v <= 0:
        raise ValueError(s)
    return f_to_studio(v)


def ev_from_str(s: str) -> int:
    v = num(s)
    if v is None:
        raise ValueError(s)
    return int(round(v * 1000))


# white balance by name -> Studio's codes (the Sony / PTP ones the screens use)
_WB_WORDS = [("auto", 2), ("daylight", 4), ("sun", 4), ("fine", 4), ("shade", 32785), ("cloud", 32784),
             ("tungsten", 6), ("incandescent", 6), ("fluor", 5), ("flash", 7), ("kelvin", 32786), ("color temp", 32786),
             ("ctemp", 32786), ("manual", 1), ("custom", 1)]


def wb_from_name(s: str) -> int:
    low = str(s).lower()
    for word, code in _WB_WORDS:
        if word in low:
            return code
    raise ValueError(s)


def jpeg_between(buf: bytes) -> bytes | None:
    """The first complete JPEG (FF D8 ... FF D9) in buf."""
    a = buf.find(b"\xff\xd8")
    if a < 0:
        return None
    b = buf.rfind(b"\xff\xd9")
    return bytes(buf[a:b + 2]) if b > a else None


class FrameStream:
    """Keeps the newest live-view JPEG from a camera that pushes a stream (UDP or a long HTTP response) in a
    background thread. live_view() then just returns the newest frame."""

    def __init__(self, reader, name: str = "camera-stream"):
        self.reader = reader                       # callable(stop_event, put) that runs until stop is set
        self.frame: bytes | None = None
        self.at = 0.0
        self.error = ""
        self._stop = threading.Event()
        self._new = threading.Condition()
        self.thread = threading.Thread(target=self._run, daemon=True, name=name)
        self.thread.start()

    def _put(self, jpg: bytes) -> None:
        with self._new:
            self.frame, self.at = jpg, time.time()
            self._new.notify_all()

    def _run(self) -> None:
        try:
            self.reader(self._stop, self._put)
        except Exception as exc:  # noqa: BLE001 - said on screen through live_view()
            self.error = str(exc) or exc.__class__.__name__

    def latest(self, timeout: float = 3.0) -> bytes:
        with self._new:
            if self.frame is None or time.time() - self.at > timeout:
                self._new.wait(timeout)
        if self.frame is None or time.time() - self.at > max(timeout, 5.0):
            raise PTPError(self.error or "No live picture from the camera yet.")
        return self.frame

    @property
    def alive(self) -> bool:
        return self.thread.is_alive() and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()


class HTTPCamera:
    """Base for the HTTP Wi-Fi drivers. Subclasses set `brand`, `default_ip`, `port`, `KEYS` and implement
    `_handshake`, `_read`, `_write`, `_shoot`, and the optional live view / movie / focus parts."""

    brand = "Camera"
    kind = "http-wifi"
    default_ip = ""
    port = 80
    timeout = 6.0
    headers: dict = {}
    # Studio id -> camera key; conversions in CONVERT (Studio id -> camera string -> Studio value)
    KEYS: dict[int, str] = {}
    CONVERT: dict = {}
    kept_on_card = False            # True when the photo stays on the camera's card (not sent to Studio)

    def __init__(self, host: str = "", port: int | None = None, **_ignored):
        self.ip = (host or self.default_ip).split(":")[0]
        if port:
            self.port = port
        self.model = ""
        self._prop_cache: dict[int, PropInfo] = {}
        self._back: dict[int, list[tuple[int, str]]] = {}
        self._raw: dict[str, tuple[str, list[str], bool]] = {}
        self.stream: FrameStream | None = None
        self.recording = False

    # -- HTTP ----------------------------------------------------------------------------------------------------------
    def url(self, path: str, **params) -> str:
        q = ("?" + urllib.parse.urlencode(params, safe="/,:")) if params else ""
        host = self.ip if self.port == 80 else f"{self.ip}:{self.port}"
        return f"http://{host}/{path.lstrip('/')}{q}"

    def http(self, path: str, method: str = "GET", body: bytes | None = None, ctype: str | None = None,
             timeout: float | None = None, **params) -> bytes:
        req = urllib.request.Request(self.url(path, **params), data=body, method=method, headers=dict(self.headers))
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            raise PTPError(f"The {self.brand} camera answered {exc.code} to {path}.") from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise PTPError(f"The {self.brand} camera at {self.ip} does not answer over Wi-Fi ({exc}).") from exc

    # -- the driver methods Studio calls -------------------------------------------------------------------------------
    @property
    def name(self) -> str:
        m = self.model or "camera"
        return m if self.brand.split(" ")[0].lower() in m.lower() else f"{self.brand} {m}"

    def connect(self, recover: bool = False) -> None:
        self._handshake()
        self.refresh()

    def close(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream = None
        try:
            self._goodbye()
        except Exception:  # noqa: BLE001
            pass

    def _goodbye(self) -> None:
        pass

    def refresh(self) -> dict[int, PropInfo]:
        self._raw = self._read()
        for sid, key in self.KEYS.items():
            if key not in self._raw:
                continue
            value, choices, writable = self._raw[key]
            conv = self.CONVERT[sid]
            if sid == S_BATTERY:
                try:
                    self._prop_cache[sid] = PropInfo(S_BATTERY, 3, False, True, int(conv(value)), range=(-1, 100, 1))
                except (TypeError, ValueError):
                    pass
                continue
            pairs = []
            for c in choices:
                try:
                    pairs.append((conv(c), c))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
            try:
                cur = conv(value)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            self._back[sid] = pairs
            dtype = 6 if sid in (S_SHUTTER, S_ISO) else 3
            self._prop_cache[sid] = PropInfo(sid, dtype, writable and bool(pairs), True, cur,
                                             choices=list(dict.fromkeys(s for s, _ in pairs)))
        return self._prop_cache

    @staticmethod
    def resolve(prop) -> int:
        if isinstance(prop, int):
            return prop
        if prop in PROPS:
            return PROPS[prop]
        return int(prop, 16)

    def set(self, prop, value: int) -> None:
        sid = self.resolve(prop)
        key = self.KEYS.get(sid)
        if not key or key not in self._raw:
            raise Unsupported(f"Studio cannot change {format_value(sid, value)} on this {self.brand} camera.")
        cam_value = next((c for s, c in self._back.get(sid, []) if s == int(value)), None)
        if cam_value is None:
            raise PTPError(f"The {self.brand} camera does not offer {format_value(sid, value)} right now.")
        self._write(key, cam_value)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            p = self.refresh().get(sid)
            if p is not None and p.value == int(value):
                return
            time.sleep(0.15)

    def try_set(self, prop, value: int) -> int:
        try:
            self.set(prop, value)
            return 0x2001
        except PTPError:
            return 0x2002

    def settings(self) -> list[dict]:
        out = []
        for p in self.refresh().values():
            e = {"name": p.name, "code": f"0x{p.code:04X}", "value": p.value, "display": format_value(p.code, p.value),
                 "writable": p.writable}
            if p.choices:
                e["choices"] = p.choices
            out.append(e)
        return out

    def capture(self, timeout: float = 20.0, af: bool = True) -> list:
        return self._shoot(timeout)

    def fetch_pending(self, timeout: float = 20.0, quiet: float = 1.5, expect=None) -> list:
        return []

    def live_view(self) -> bytes:
        if self.stream is None or not self.stream.alive:
            self.stream = self._start_stream()
        return self.stream.latest()

    def _start_stream(self) -> FrameStream:
        raise Unsupported(f"This {self.brand} camera gives no live picture over Wi-Fi in Studio.")

    def movie_record(self, start: bool) -> None:
        raise Unsupported(f"Studio cannot start or stop video on this {self.brand} camera over Wi-Fi yet.")

    def zoom(self, direction: int) -> None:
        raise Unsupported("No power zoom control on this camera.")

    def zoom_position(self) -> int | None:
        return None

    def focus_step(self, step: int) -> None:
        raise Unsupported(f"Studio cannot move the focus on this {self.brand} camera yet.")

    def autofocus(self, hold: float = 0.5) -> None:
        self.focus_at(0.5, 0.5)

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        raise Unsupported(f"Tap to focus is not available on this {self.brand} camera yet.")

    def set_af_point(self, xf: float, yf: float) -> None:
        self.focus_at(xf, yf)

    def capabilities(self) -> dict:
        return {"live_view": True, "capture": True, "movie": False, "focus": False, "zoom": False,
                "settings": sorted(f"0x{c:04X}" for c in self._prop_cache)}

    # -- for subclasses ------------------------------------------------------------------------------------------------
    def _handshake(self) -> None:
        raise NotImplementedError

    def _read(self) -> dict[str, tuple[str, list[str], bool]]:
        raise NotImplementedError

    def _write(self, key: str, value: str) -> None:
        raise NotImplementedError

    def _shoot(self, timeout: float) -> list:
        raise NotImplementedError


def udp_socket(port: int = 0, timeout: float = 1.0) -> socket.socket:
    """A UDP socket for a camera's live-view stream (port 0 = any free port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind(("", port))
    s.settimeout(timeout)
    return s
