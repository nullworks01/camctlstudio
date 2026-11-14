"""OM System / Olympus over Wi-Fi (the camera's own remote protocol, the one OI.Share / OM Image Share uses).

UNTESTED ON REAL CAMERAS. Written from public sources (the "OPC" protocol notes collected for the E-M10 and TG-5 by
open-source projects such as olympus-wifi and libqt-omd; names and formats only, no code copied) and tested against a
simulated camera only.

The camera is its own access point at 192.168.0.10. Every command is an HTTP request with the headers
"Host: 192.168.0.10" and "User-Agent: OI.Share v2":
  get_caminfo.cgi (model), switch_cammode.cgi?mode=rec|play, get_camprop.cgi?com=desc&propname=desclist (every
  setting with its allowed values), set_camprop.cgi?com=set&propname=X (POST <set><value>V</value></set>),
  exec_takemotion.cgi?com=starttake (shutter), ...com=assignafframe&point=XXXXxYYYY (focus point),
  exec_takemisc.cgi?com=startliveview&port=N (live picture as RTP over UDP, one JPEG split over several packets).
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from .httpcam import (FrameStream, HTTPCamera, PTPError, S_APERTURE, S_EV, S_EXPOSURE_MODE, S_ISO, S_SHUTTER,
                      Unsupported, ev_from_str, f_from_str, iso_from_str, jpeg_between, udp_socket)
from .ptpstd import seconds_to_studio

DEFAULT_IP = "192.168.0.10"
LV_SIZE = (640, 480)


def oly_shutter(s: str) -> int:
    """'250' = 1/250 s, '1/250' too; '2"' = 2 s, '1"3' = 1.3 s; 'Bulb' / 'LIVETIME' = bulb (0)."""
    t = str(s).strip()
    if not t or t.lower() in ("bulb", "livetime", "livecomp", "time"):
        return 0
    if '"' in t:
        a, _, b = t.partition('"')
        return seconds_to_studio(float(f"{a}.{b or 0}"))
    if "/" in t:
        n, _, d = t.partition("/")
        return seconds_to_studio(float(n) / float(d))
    v = float(t)
    return seconds_to_studio(1 / v if v >= 1 else v)


# Studio's exposure mode codes (the PTP ones: 1 M, 2 P, 3 A, 4 S)
TAKEMODE = {"M": 1, "P": 2, "A": 3, "S": 4, "iAuto": 0x8000, "ART": 0x8001, "movie": 0x8002}


def oly_takemode(s: str) -> int:
    if s not in TAKEMODE:
        raise ValueError(s)
    return TAKEMODE[s]


def rtp_payload(pkt: bytes) -> tuple[bytes, int, bool]:
    """(payload, timestamp, last packet of the frame) of one RTP packet."""
    if len(pkt) < 12:
        return b"", 0, False
    cc, ext, marker = pkt[0] & 0x0F, pkt[0] & 0x10, bool(pkt[1] & 0x80)
    ts = int.from_bytes(pkt[4:8], "big")
    i = 12 + 4 * cc
    if ext and len(pkt) >= i + 4:
        i += 4 + 4 * int.from_bytes(pkt[i + 2:i + 4], "big")
    return pkt[i:], ts, marker


class OlympusCamera(HTTPCamera):
    brand = "OM System / Olympus"
    kind = "olympus-wifi"
    default_ip = DEFAULT_IP
    headers = {"Host": DEFAULT_IP, "User-Agent": "OI.Share v2"}
    KEYS = {S_SHUTTER: "shutspeedvalue", S_APERTURE: "focalvalue", S_ISO: "isospeedvalue", S_EV: "expcomp",
            S_EXPOSURE_MODE: "takemode"}
    CONVERT = {S_SHUTTER: oly_shutter, S_APERTURE: f_from_str, S_ISO: iso_from_str, S_EV: ev_from_str,
               S_EXPOSURE_MODE: oly_takemode}

    def __init__(self, host: str = "", port: int | None = None, **kw):
        super().__init__(host, port)
        self.mode = ""
        self.lv_port = 0

    @property
    def name(self) -> str:
        m = self.model or "camera"
        return m if any(w in m.upper() for w in ("OLYMPUS", "OM SYSTEM", "OM-")) else f"OM System / Olympus {m}"

    def _switch(self, mode: str) -> None:
        if self.mode != mode:
            params = {"mode": mode}
            if mode == "rec":
                params["lvqty"] = f"{LV_SIZE[0]:04d}x{LV_SIZE[1]:04d}"
            self.http("switch_cammode.cgi", **params)
            self.mode = mode
            if mode != "rec" and self.stream:
                self.stream.stop()
                self.stream = None

    def _handshake(self) -> None:
        text = self.http("get_caminfo.cgi").decode("utf-8", "replace")
        m = re.search(r"<model>([^<]+)</model>", text)
        self.model = m.group(1).strip() if m else "camera"
        self._switch("rec")

    def _read(self) -> dict:
        self._switch("rec")
        root = ET.fromstring(self.http("get_camprop.cgi", com="desc", propname="desclist"))
        out = {}
        for desc in root.iter("desc"):
            f = {c.tag: (c.text or "").strip() for c in desc}
            if "propname" in f:
                out[f["propname"]] = (f.get("value", ""), f.get("enum", "").split(), "set" in f.get("attribute", ""))
        return out

    def _write(self, key: str, value: str) -> None:
        self._switch("rec")
        body = f'<?xml version="1.0"?>\r\n<set>\r\n<value>{value}</value>\r\n</set>\r\n'.encode()
        self.http("set_camprop.cgi", method="POST", body=body, ctype="text/plain;charset=utf-8", com="set",
                  propname=key)

    def _shoot(self, timeout: float) -> list:
        self._switch("rec")
        text = self.http("exec_takemotion.cgi", com="starttake").decode("utf-8", "replace")
        if "<take>" in text and "<take>ok" not in text:
            raise PTPError("The camera did not take the picture (it could not focus, or the card is full).")
        return self._newest_files(timeout)

    def _newest_files(self, timeout: float) -> list:
        """The photo just taken, from the card (play mode lists and serves the files)."""
        time.sleep(1.0)
        self._switch("play")
        try:
            dirs = self._list("/DCIM")
            if not dirs:
                return []
            folder = sorted(d for d in dirs if d[3] & 0x10 or "." not in d[1])[-1] if dirs else None
            files = self._list(f"{folder[0]}/{folder[1]}") if folder else []
            if not files:
                return []
            newest = max(files, key=lambda r: (r[4], r[5], r[1]))
            stem = newest[1].rsplit(".", 1)[0]
            out = []
            for r in files:                                    # JPEG and RAW of the same shot
                if r[1].rsplit(".", 1)[0] == stem:
                    data = self.http(f"{r[0].lstrip('/')}/{r[1]}", timeout=max(20.0, timeout))
                    out.append((0x3801 if r[1].upper().endswith(".JPG") else 0x3000, data))
            return out
        finally:
            self._switch("rec")

    def _list(self, path: str) -> list[tuple]:
        try:
            text = self.http("get_imglist.cgi", DIR=path).decode("utf-8", "replace")
        except PTPError:
            return []
        rows = []
        for line in text.splitlines():
            p = line.split(",")
            if len(p) >= 6 and p[0].startswith("/"):
                try:
                    rows.append((p[0], p[1], int(p[2]), int(p[3]), int(p[4]), int(p[5])))
                except ValueError:
                    continue
        return rows

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        self._switch("rec")
        x, y = int(max(0, min(1, xf)) * LV_SIZE[0]), int(max(0, min(1, yf)) * LV_SIZE[1])
        text = self.http("exec_takemotion.cgi", com="assignafframe", point=f"{x:04d}x{y:04d}").decode("utf-8", "replace")
        return {"ok": "<affocus>ok" in text, "x": xf, "y": yf}

    def movie_record(self, start: bool) -> None:
        raise Unsupported("Studio cannot start video on an OM System / Olympus camera over Wi-Fi yet.")

    def _start_stream(self) -> FrameStream:
        self._switch("rec")
        sock = udp_socket(0, timeout=1.0)
        self.lv_port = sock.getsockname()[1]
        self.http("exec_takemisc.cgi", com="startliveview", port=str(self.lv_port))

        def reader(stop, put):
            parts, ts_now = [], None
            try:
                while not stop.is_set():
                    try:
                        pkt, _ = sock.recvfrom(65536)
                    except OSError:
                        continue
                    payload, ts, last = rtp_payload(pkt)
                    if ts != ts_now:
                        parts, ts_now = [], ts
                    parts.append(payload)
                    if last:
                        jpg = jpeg_between(b"".join(parts))
                        if jpg:
                            put(jpg)
                        parts = []
            finally:
                sock.close()
                try:
                    self.http("exec_takemisc.cgi", com="stopliveview")
                except PTPError:
                    pass

        return FrameStream(reader, name="olympus-live")

    def _goodbye(self) -> None:
        self._switch("play")

    def capabilities(self) -> dict:
        d = super().capabilities()
        d.update(focus=True)
        return d
