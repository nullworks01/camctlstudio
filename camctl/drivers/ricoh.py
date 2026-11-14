"""Ricoh GR II / GR III / GR IIIx and Pentax bodies with the same Wi-Fi API (K-1, K-1 II, KP, K-3 III).

UNTESTED ON REAL CAMERAS. Written from the public OpenAPI description of the camera's HTTP API
(CursedHardware/ricoh-wireless-protocol, reverse engineered from the Image Sync app; names only, no code copied) and
tested against a simulated camera only.

The camera is its own access point at 192.168.0.1. JSON over HTTP under /v1/:
  GET /v1/props (model, battery, av / tv / sv / xv / WBMode / exposureMode with their *List of allowed values),
  PUT /v1/params/camera (form body, e.g. av=5.6), POST /v1/camera/shoot, POST /v1/lens/focus (pos=x,y),
  GET /v1/liveview (an endless M-JPEG stream), GET /v1/photos and /v1/photos/<folder>/<file>.
Values: av "2.8"; tv "1.250" means 1/250 s and "2.1" means 2 s (numerator.denominator); sv "auto" or "400";
xv "-0.3".
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .httpcam import (FrameStream, HTTPCamera, PTPError, S_APERTURE, S_BATTERY, S_EV, S_ISO, S_SHUTTER, S_WB,
                      ev_from_str, f_from_str, iso_from_str, wb_from_name)
from .ptpstd import seconds_to_studio

DEFAULT_IP = "192.168.0.1"


def ricoh_shutter(s: str) -> int:
    t = str(s).strip()
    if t.lower() in ("bulb", "time", ""):
        return 0
    n, sep, d = t.partition(".")
    if not sep:
        return seconds_to_studio(float(n))
    return seconds_to_studio(float(n) / float(d))


class RicohCamera(HTTPCamera):
    brand = "Ricoh / Pentax"
    kind = "ricoh-wifi"
    default_ip = DEFAULT_IP
    KEYS = {S_APERTURE: "av", S_SHUTTER: "tv", S_ISO: "sv", S_EV: "xv", S_WB: "WBMode", S_BATTERY: "battery"}
    CONVERT = {S_APERTURE: f_from_str, S_SHUTTER: ricoh_shutter, S_ISO: iso_from_str, S_EV: ev_from_str,
               S_WB: wb_from_name, S_BATTERY: int}

    def __init__(self, host: str = "", port: int | None = None, **kw):
        super().__init__(host, port)
        self.props: dict = {}

    @property
    def name(self) -> str:
        m = self.model or "camera"
        return m if any(w in m.upper() for w in ("RICOH", "PENTAX")) else f"Ricoh / Pentax {m}"

    def api(self, path: str, method: str = "GET", form: dict | None = None, timeout: float | None = None) -> dict:
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        raw = self.http(f"v1/{path}", method=method, body=body,
                        ctype="application/x-www-form-urlencoded" if body is not None else None, timeout=timeout)
        try:
            d = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            return {}
        if isinstance(d, dict) and d.get("errCode") not in (None, 200):
            raise PTPError(f"The camera refused {path}: {d.get('errMsg') or d.get('errCode')}.")
        return d if isinstance(d, dict) else {}

    def _handshake(self) -> None:
        self.props = self.api("props")
        self.model = self.props.get("model", "") or "camera"

    def _read(self) -> dict:
        self.props = self.api("props")
        p, out = self.props, {}
        for key in ("av", "tv", "sv", "xv", "WBMode"):
            if key in p:
                out[key] = (str(p[key]), [str(v) for v in p.get(key + "List") or []], True)
        if "battery" in p:
            out["battery"] = (str(p["battery"]), [], False)
        self.recording = p.get("stateMovie") in ("recording", "busy") if p.get("captureMode") == "movie" else False
        return out

    def _write(self, key: str, value: str) -> None:
        self.api("params/camera", method="PUT", form={key: value})

    def _shoot(self, timeout: float) -> list:
        before = self._photo_names()
        r = self.api("camera/shoot", method="POST", form={"af": "camera"})
        if r.get("captured") is False:
            raise PTPError("The camera did not take the picture (it could not focus, or the card is full).")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.8)
            new = [n for n in self._photo_names() if n not in before]
            if new:
                out = []
                stem = new[-1][1].rsplit(".", 1)[0]
                for folder, f in new:
                    if f.rsplit(".", 1)[0] == stem:
                        data = self.http(f"v1/photos/{folder}/{f}", timeout=max(30.0, timeout), size="full")
                        out.append((0x3801 if f.upper().endswith(".JPG") else 0x3000, data))
                return out
        return []

    def _photo_names(self) -> list[tuple[str, str]]:
        try:
            d = self.api("photos")
        except PTPError:
            return []
        return [(x.get("name", ""), f) for x in d.get("dirs") or [] for f in x.get("files") or []]

    def movie_record(self, start: bool) -> None:
        if self.props.get("captureMode") != "movie":
            raise PTPError("Switch the camera to movie mode first (on the camera).")
        self.api("camera/shoot/start" if start else "camera/shoot/finish", method="POST")
        self.recording = start

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        x, y = int(max(0, min(1, xf)) * 100), int(max(0, min(1, yf)) * 100)
        r = self.api("lens/focus", method="POST", form={"pos": f"{x},{y}"})
        return {"ok": bool(r.get("focused", True)), "x": xf, "y": yf}

    def _start_stream(self) -> FrameStream:
        url = self.url("v1/liveview")

        def reader(stop, put):
            with urllib.request.urlopen(url, timeout=10) as r:
                buf = bytearray()
                while not stop.is_set():
                    chunk = r.read(16384)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                        if a < 0 or b < 0:
                            if a > 0:
                                del buf[:a]
                            break
                        put(bytes(buf[a:b + 2]))
                        del buf[:b + 2]
                    if len(buf) > 4 << 20:
                        buf.clear()

        return FrameStream(reader, name="ricoh-live")

    def capabilities(self) -> dict:
        d = super().capabilities()
        d.update(movie=True, focus=True)
        return d
