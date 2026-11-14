"""Panasonic LUMIX over Wi-Fi (the camera's own remote protocol, the one the LUMIX Sync / Image App uses).

UNTESTED ON REAL CAMERAS. Written from public sources (libgphoto2 camlibs/lumix/lumix.c, LGPL, numbers and command
names only; open-source LUMIX remote projects) and tested against a simulated camera only.

Every command is an HTTP GET to http://<camera>/cam.cgi?mode=... with an XML answer
(<camrply><result>ok</result>...). Settings are strings: shutter and aperture as APEX values in 1/256 steps
("2560/256" = Tv 10 = 1/1000 s, "1280/256" = Av 5 = f/5.6), ISO as "auto" or the number.
The live picture comes as UDP packets, each one a short header and one JPEG, after mode=startstream; the stream
stops unless the camera is asked for its state every few seconds.
The photo stays on the camera's card (Studio does not copy it over Wi-Fi from this camera yet).
"""
from __future__ import annotations

import re
import time
import uuid

from .httpcam import (FrameStream, HTTPCamera, PTPError, S_APERTURE, S_BATTERY, S_ISO, S_SHUTTER, Unsupported,
                      f_from_str, iso_from_str, jpeg_between, udp_socket)
from .ptpstd import seconds_to_studio

DEFAULT_IP = "192.168.54.1"           # the camera's own access point


def apex_shutter(s: str) -> int:
    """'2560/256' (Tv = 10) -> 1/1000 s in Studio's units."""
    n, _, d = str(s).partition("/")
    tv = float(n) / float(d or 1)
    return seconds_to_studio(2 ** -tv)


def apex_aperture(s: str) -> int:
    """'1280/256' (Av = 5) -> f/5.6 in Studio's units."""
    n, _, d = str(s).partition("/")
    av = float(n) / float(d or 1)
    return f_from_str(str(2 ** (av / 2)))


# the values the camera understands (1/3 stops), as the Image App sends them
SHUTTERS = [f"{f - d}/256" for f in range(3328, -1536, -256) for d in (0, 85, 170)] + ["-1536/256"]  # 1/8000-60 s
APERTURES = ["392/256", "427/256", "512/256", "598/256", "683/256", "768/256", "854/256", "938/256", "1024/256",
             "1110/256", "1195/256", "1280/256", "1366/256", "1451/256", "1536/256", "1622/256", "1707/256",
             "1792/256", "1878/256", "1963/256", "2048/256"]
ISOS = ["auto", "100", "125", "160", "200", "250", "320", "400", "500", "640", "800", "1000", "1250", "1600", "2000",
        "2500", "3200", "4000", "5000", "6400", "8000", "10000", "12800", "16000", "20000", "25600"]


def battery_pct(s: str) -> int:
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(s))
    if not m or int(m.group(2)) == 0:
        raise ValueError(s)
    return int(round(100 * int(m.group(1)) / int(m.group(2))))


class LumixCamera(HTTPCamera):
    brand = "Panasonic"
    kind = "lumix-wifi"
    default_ip = DEFAULT_IP
    kept_on_card = True
    KEYS = {S_SHUTTER: "shtrspeed", S_APERTURE: "focal", S_ISO: "iso", S_BATTERY: "batt"}
    CONVERT = {S_SHUTTER: apex_shutter, S_APERTURE: apex_aperture, S_ISO: iso_from_str, S_BATTERY: battery_pct}
    LISTS = {"shtrspeed": SHUTTERS, "focal": APERTURES, "iso": ISOS}

    def __init__(self, host: str = "", port: int | None = None, name: str = "camctl", guid: bytes | None = None,
                 **kw):
        super().__init__(host, port)
        self.client_name = name
        self.device_id = str(uuid.UUID(bytes=guid)) if guid and len(guid) == 16 else str(uuid.uuid4())
        self._state_at = 0.0

    def cgi(self, mode: str, check: bool = True, **params) -> str:
        text = self.http("cam.cgi", mode=mode, **params).decode("utf-8", "replace")
        if check:
            res = re.search(r"<result>([^<]*)</result>", text)
            if res and res.group(1).strip() not in ("ok",):
                raise PTPError(f"The LUMIX camera refused {mode} ({res.group(1).strip()}).")
        return text

    def _handshake(self) -> None:
        deadline = time.time() + 45
        while True:
            text = self.cgi("accctrl", check=False, type="req_acc", value=self.device_id, value2=self.client_name)
            if "ok_under_research" not in text:
                break
            if time.time() > deadline:                 # the camera shows a prompt: he confirms it there
                raise PTPError("Confirm the connection on the LUMIX camera's screen, then Studio connects.")
            time.sleep(1.0)
        if "err" in text.split("<result>")[-1][:12] and "ok" not in text:
            raise PTPError("The LUMIX camera refused the connection. Pair it once in the LUMIX Sync / Image App "
                           "style Wi-Fi menu (Wi-Fi › Wi-Fi Function › Remote Shooting), then try again.")
        self.cgi("camcmd", value="recmode")
        state = self.cgi("getstate", check=False)
        m = re.search(r"<(?:model|modelname|name)>([^<]+)</", state)
        if not self.model:
            self.model = m.group(1).strip() if m else "LUMIX"

    def _keepalive(self) -> None:
        if time.time() - self._state_at > 3:
            self._state_at = time.time()
            try:
                self.cgi("getstate", check=False)
            except PTPError:
                pass

    def _read(self) -> dict:
        out = {}
        for key in ("shtrspeed", "focal", "iso"):
            try:
                text = self.cgi("getsetting", type=key)
            except PTPError:
                continue
            m = re.search(rf'{key}="([^"]*)"', text)
            if m:
                out[key] = (m.group(1), self.LISTS[key], True)
        try:
            state = self.cgi("getstate", check=False)
            self._state_at = time.time()
            b = re.search(r"<batt>([^<]+)</batt>", state)
            if b:
                out["batt"] = (b.group(1), [], False)
            self.recording = "<rec>on</rec>" in state
        except PTPError:
            pass
        return out

    def _write(self, key: str, value: str) -> None:
        self.cgi("setsetting", type=key, value=value)

    def _shoot(self, timeout: float) -> list:
        self.cgi("camcmd", value="capture")
        time.sleep(0.3)
        try:
            self.cgi("camcmd", check=False, value="capture_cancel")
        except PTPError:
            pass
        return []                                      # the photo is on the camera's card

    def movie_record(self, start: bool) -> None:
        self.cgi("camcmd", value="video_recstart" if start else "video_recstop")
        self.recording = start

    def zoom(self, direction: int) -> None:
        self.cgi("camcmd", value={1: "tele-normal", -1: "wide-normal"}.get(direction, "zoomstop"))

    def focus_step(self, step: int) -> None:
        word = "wide" if step < 0 else "tele"
        speed = "fast" if abs(step) > 1 else "normal"
        text = self.cgi("camctrl", check=False, type="focus", value=f"{word}-{speed}")
        if "err" in text and "ok" not in text:
            raise Unsupported("The LUMIX camera moves the focus only in MF: set the focus mode to MF.")

    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict:
        x, y = int(max(0, min(1, xf)) * 1000), int(max(0, min(1, yf)) * 1000)
        self.cgi("camctrl", check=False, type="touch", value=f"{x}/{y}", value2="on")
        return {"ok": True, "x": xf, "y": yf}

    def _start_stream(self) -> FrameStream:
        sock = udp_socket(0, timeout=1.0)
        port = sock.getsockname()[1]
        self.cgi("startstream", check=False, value=str(port))

        def reader(stop, put):
            last = time.time()
            try:
                while not stop.is_set():
                    self._keepalive()
                    try:
                        data, _ = sock.recvfrom(65536)
                    except OSError:
                        if time.time() - last > 4:            # stalled: ask again
                            self.cgi("startstream", check=False, value=str(port))
                            last = time.time()
                        continue
                    jpg = jpeg_between(data)
                    if jpg:
                        last = time.time()
                        put(jpg)
            finally:
                sock.close()
                try:
                    self.cgi("stopstream", check=False)
                except PTPError:
                    pass

        return FrameStream(reader, name="lumix-live")

    def _goodbye(self) -> None:
        self.cgi("camcmd", check=False, value="playmode")

    def capabilities(self) -> dict:
        d = super().capabilities()
        d.update(movie=True, focus=True, zoom=True, photo_on_card=True)
        return d
