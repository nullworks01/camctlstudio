"""camctl Studio: a visual control panel for the camera, in the browser.

Run:  python -m camctl.webui          then open http://127.0.0.1:8770
      python -m camctl.webui --host usb|<camera-ip> --port 8770

Listens on this PC only (127.0.0.1). It holds the camera connection while it
runs, so close it before using the camera from Claude Desktop.
"""
from __future__ import annotations

import base64
import io
import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import webbrowser
import zlib
from collections import deque
from pathlib import Path

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .drivers.sony import (SP_FLASH_COMP, SP_DRO, SP_IMAGE_SIZE, SP_SHUTTER, SP_STATUS, SP_COLOR_TEMP, SP_WB_SHIFT_GM, SP_ASPECT, SP_FOCUS_INDICATION, SP_PENDING_IMAGES, SP_AF_LOCK, SP_BATTERY, SP_PICTURE_EFFECT, SP_WB_SHIFT_AB, SP_REC_STATE, SP_ISO, SP_SAVE_TO, SP_FOCUS_AREA, SP_PICTURE_PROFILE, SP_MOVIE_FORMAT, SP_MOVIE_SETTING, SP_SHOOT_CLASS, SP_MEDIA_STATUS, SP_SHOTS_LEFT, SP_REC_TIME_LEFT, SP_FOCUS_POSITION, SP_STILL_FORMAT, SP_ZOOM_POSITION, SP_JPEG_QUALITY, SP_LIVE_VIEW_QUALITY, SC_STILL_D2C7)  # noqa: E402,F401  Sony codes by name
from . import liveroute  # noqa: E402
from . import assistant, audiocheck, autofix, capture_card, looks, playground, reel, camprofile, automation, capture, coach, director, followlight, formats, handoff, importer, lut, measure, presets, scene, setups, shotplan, smartphoto, video, wbauto, wifi
from .drivers.sony import PROPS, SonyCamera, format_value
from .drivers.ptpstd import Unsupported  # noqa: E402  (a driver that cannot do something, not a lost link)
from .ptpip import PTPError

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"
CAMCHECK_AUDIT = Path(__file__).resolve().parent.parent / "AUDIT.md"    # the camera check's result (the simulator sends it elsewhere)
_real_sleep = time.sleep      # background loops keep sleeping even when a test patches time.sleep

WB = {2: "Auto", 4: "Daylight", 32785: "Shade", 32784: "Cloudy", 6: "Incandescent",
      32769: "Fluor. warm white", 32770: "Fluor. cool white", 32771: "Fluor. day white",
      32772: "Fluor. daylight", 7: "Flash", 32816: "Underwater auto", 32786: "Color temp",
      32800: "Custom 1", 32801: "Custom 2", 32802: "Custom 3"}
FOCUS = {1: "MF", 2: "AF-S", 32772: "AF-C", 32773: "AF-A", 32774: "DMF"}
MODE = {1: "Manual (M)", 65538: "Program (P)", 131075: "Aperture (A)", 196612: "Shutter (S)"}
LABELS = {"white_balance": WB, "focus_mode": FOCUS, "exposure_mode": MODE}
CONTROLS = ["exposure_mode", "aperture", "shutter_speed", "iso",
            "exposure_compensation", "white_balance", "color_temp", "focus_mode", "focus_area", "picture_profile",
            "movie_format", "movie_setting", "still_format", "save_to", "jpeg_quality", "image_size"]
EXTRA_CODES = {"color_temp": SP_COLOR_TEMP, "picture_profile": SP_PICTURE_PROFILE, "focus_area": SP_FOCUS_AREA,
               "movie_format": SP_MOVIE_FORMAT, "movie_setting": SP_MOVIE_SETTING,
               "still_format": SP_STILL_FORMAT, "save_to": SP_SAVE_TO, "jpeg_quality": SP_JPEG_QUALITY, "image_size": SP_IMAGE_SIZE}
# Verified on the ZV-E10 with test shots / side effects: 2 gave RAW+JPEG, 3 unlocks JPEG-only
# features (Picture Effect, JPEG quality); SP_SAVE_TO = 16 kept photos on the card only.
STILL_FORMAT = {1: "RAW", 2: "RAW + JPEG", 3: "JPEG"}
SAVE_TO = {1: "PC only", 16: "Card only", 17: "PC + card"}
JPEG_QUALITY = {1: "Standard", 3: "Fine", 4: "Extra fine"}
IMAGE_SIZE = {1: "L", 2: "M", 3: "S"}
PHOTO_ONLY = {"still_format", "save_to", "jpeg_quality", "image_size"}
PROP_REC_STATE, PROP_REC_TIME_LEFT, PROP_SHOTS_LEFT = SP_REC_STATE, SP_REC_TIME_LEFT, SP_SHOTS_LEFT
MOVIE_FORMAT = {8: "XAVC S 4K", 9: "XAVC S HD"}
# Sony record-setting codes (from Sony's remote SDK numbering; matched on the ZV-E10 by
# the remaining record time each one reports).
MOVIE_SETTING = {1: "60p 50M", 2: "30p 50M", 3: "24p 50M", 4: "50p 50M", 5: "25p 50M",
                 0x18: "60p 25M", 0x19: "50p 25M", 0x1A: "30p 16M", 0x1B: "25p 16M",
                 0x1C: "120p 100M", 0x1D: "100p 100M", 0x1E: "120p 60M", 0x1F: "100p 60M",
                 0x20: "4K 30p 100M", 0x21: "4K 25p 100M", 0x22: "4K 24p 100M",
                 0x23: "4K 30p 60M", 0x24: "4K 25p 60M", 0x25: "4K 24p 60M"}
FOCUS_AREA = {1: "Wide", 2: "Zone", 3: "Center", 257: "Spot S", 258: "Spot M", 259: "Spot L", 260: "Expand Spot"}
PROP_BATTERY = SP_BATTERY
# Names for the "All settings" panel. Codes marked (?) are educated guesses from
# Sony's PTP documentation and tests on the ZV-E10; the rest were verified.
KNOWN_PROPS = {
    0x5005: "White balance", 0x5007: "Aperture", 0x500A: "Focus mode", 0x500B: "Metering mode (?)",
    0x500C: "Flash mode (?)", 0x500E: "Exposure / shooting mode", 0x5010: "Exposure compensation",
    0x5013: "Drive mode", SP_FLASH_COMP: "Flash compensation (?)", SP_DRO: "DRO / Auto HDR (?)",
    SP_IMAGE_SIZE: "Image size (L/M/S)", SP_STILL_FORMAT: "Photo file format", SP_JPEG_QUALITY: "JPEG quality", SP_SHUTTER: "Shutter speed", SP_STATUS: "Status (?)", SP_COLOR_TEMP: "Colour temperature",
    SP_WB_SHIFT_GM: "WB shift green/magenta", SP_ASPECT: "Aspect ratio (?)", SP_FOCUS_INDICATION: "Focus indication (?)",
    SP_PENDING_IMAGES: "Pending images", SP_AF_LOCK: "AF lock state (?)", SP_BATTERY: "Battery level", SP_PICTURE_EFFECT: "Picture effect",
    SP_WB_SHIFT_AB: "WB shift amber/blue", SP_REC_STATE: "Recording state (?)", SP_ISO: "ISO", SP_SAVE_TO: "Photo save destination",
    SP_FOCUS_AREA: "Focus area", SP_PICTURE_PROFILE: "Picture Profile", SP_MOVIE_FORMAT: "Movie file format (?)",
    SP_MOVIE_SETTING: "Movie record setting (?)", SP_SHOOT_CLASS: "Shooting mode class (Still/Movie/S&Q)",
    SP_MEDIA_STATUS: "Media status (?)", SP_SHOTS_LEFT: "Remaining shots (?)", SP_REC_TIME_LEFT: "Remaining record time (?)",
}
MODE.update({0x78050: "Movie P", 0x78051: "Movie A", 0x78052: "Movie S", 0x78053: "Movie M",
             0x98059: "S&Q P", 0x9805A: "S&Q A", 0x9805B: "S&Q S", 0x9805C: "S&Q M"})
PRO_PAGE = Path(__file__).with_name("studio_pro.html")


def code_of(name: str) -> int:
    return EXTRA_CODES.get(name) or PROPS[name]


def label(name: str, value) -> str:
    if name in LABELS:
        return LABELS[name].get(value, str(value))
    if name == "color_temp":
        return f"{value} K"
    if name == "picture_profile":
        return presets.PP_NAMES.get(value, str(value))
    if name == "focus_area":
        return FOCUS_AREA.get(value, str(value))
    if name == "movie_format":
        return MOVIE_FORMAT.get(value, str(value))
    if name == "movie_setting":
        return MOVIE_SETTING.get(value, f"code {value}")
    table = {"still_format": STILL_FORMAT, "save_to": SAVE_TO, "jpeg_quality": JPEG_QUALITY,
             "image_size": IMAGE_SIZE}.get(name)
    if table is not None:
        return table.get(value, str(value))
    return format_value(PROPS[name], value)


class _Backoff(PTPError):
    """Raised while waiting before the next reconnect attempt (keeps last_error as the real cause)."""


class Studio:
    def __init__(self, host: str):
        self.host = host
        self.cam: SonyCamera | None = None
        self.lock = threading.Lock()
        self.last_error = ""
        self.recording = False
        self.rec_since = 0.0
        self.rec_toggled = 0.0
        self.rec_disagree = False
        self.rec_check = None          # {"since", "left0", "flag"}: the camera disagrees with Studio's own intent
        # Live-view pump: one background thread pulls frames back to back and every
        # browser request gets the newest one (no per-frame request round trips, and
        # other camera commands get priority between frames).
        self.waiters = 0
        self.frame: bytes | None = None
        self.frame_seq = 0
        self.frame_error = ""
        self.no_picture = ""                 # why a connected camera shows no picture (said on the screen)
        self.picture_route: dict | None = None   # liveroute.choose() for a camera without PTP live view
        self._route_at, self._route_busy = 0.0, False
        self.fps = 0.0
        self.last_view_req = 0.0
        self.released = False
        self.frame_cond = threading.Condition()
        self._props = None
        self._props_at = 0.0
        self._fails = 0
        self._retry_at = 0.0
        self.fail_since = 0.0
        self.last_ok = 0.0
        self._present_at = 0.0
        self._present_cache: bool | None = None
        self._rescan_at = 0.0
        # how to find the camera: "auto" (the saved route first, then the other one) or his fixed "usb" / "wifi"
        try:
            self.choice = wifi.load().get("choice", "auto")
        except Exception:  # noqa: BLE001
            self.choice = "auto"
        self.route_at = 0.0
        threading.Thread(target=self._pump, daemon=True, name="liveview-pump").start()

    @property
    def cam(self):
        return self._cam

    @cam.setter
    def cam(self, value) -> None:
        self._cam = value
        if value is not None:                     # RAW files are named by the maker of the connected camera
            ACTIVE_BRAND["brand"] = getattr(value, "brand", None) or "Sony"

    # -- connection health / auto-recovery ---------------------------------
    RETRY_MAX_S = 10.0

    def camera(self) -> SonyCamera:
        if self.cam is not None:
            return self.cam
        try:
            return self._connect_current()
        except _Backoff:
            raise
        except Exception:
            if self.choice != "auto":
                raise
            alt = self._other_route()                 # only when the other route really answers: no retry storm
            if not alt or alt == self.host:
                raise
            self.host = alt
            self._fails, self._retry_at = 0, 0.0
            self._present_at = 0.0
            self.route_at = time.time()
            wifi.save(mode="usb" if alt == "usb" else "wifi", **({} if alt == "usb" else {"host": alt}))   # remember it
            return self._connect_current()

    def _other_route(self) -> str | None:
        """Where else is the camera? Wi-Fi if the saved address answers, USB if a camera is on the bus."""
        try:
            if self.host == "usb":
                h = wifi.load().get("host")
                return h if h and h != "usb" and wifi.reachable(h, timeout=0.6) else None
            return "usb" if _camera_on_usb() else None
        except Exception:  # noqa: BLE001
            return None

    def _connect_current(self) -> SonyCamera:
        if self.cam is None:
            now = time.time()
            if now < self._retry_at:
                raise _Backoff(self.health()["message"] or "Camera not connected")
            last_exc: Exception | None = None
            if self.host != "usb" and self._fails and not self._wifi_ready():
                self._fails += 1
                self._retry_at = time.time() + min(2.0 ** self._fails, self.RETRY_MAX_S)
                self._mark_fail()
                self.last_error = "Camera not reachable on Wi-Fi" + (f" ({self.host})" if self.host else "")
                raise PTPError(self.last_error)
            # 1) plain connect  2) reset a camera stuck mid-transfer  3) re-enumerate USB
            for step in ("plain", "ptp_reset", "usb_reset"):
                if step != "plain" and self.host != "usb":
                    break
                cam = self._new_cam()
                try:
                    if step == "usb_reset":
                        try:
                            cam.client.connect()
                            cam.client.usb_reset()
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(2.0)
                        cam = self._new_cam()
                    cam.connect(recover=step != "plain")
                    self.cam = cam
                    self._fails = 0
                    self._retry_at = 0.0
                    _best_live_view_quality(cam)
                    self.rescue_waiting(cam, "on connect")
                    return cam
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self.last_error = str(exc)
            self._fails += 1
            self._retry_at = time.time() + min(2.0 ** self._fails, self.RETRY_MAX_S)
            self._mark_fail()
            raise last_exc if last_exc else PTPError("Camera not connected")
        return self.cam

    def _new_cam(self) -> SonyCamera:
        if self.host == "usb":
            # the camera on the cable picks the driver (a Sony always gets the Sony driver, as before);
            # CAMCTL_ROUTE=gphoto2 uses the optional gphoto2 route where it is installed (Linux / macOS)
            from .drivers import registry
            return registry.make_usb_driver(prefer=os.environ.get("CAMCTL_ROUTE"), sony=SonyCamera)
        # Wi-Fi: the same GUID every time (the camera paired with it); a long first wait
        # leaves time to confirm the pairing prompt on the camera.
        ip, port = wifi.split(self.host)
        return SonyCamera(ip, port, guid=wifi.guid(), name=wifi.client_name(), timeout=60.0)

    def _wifi_ready(self) -> bool:
        """Is the camera reachable on Wi-Fi? If not, look for it again (its IP may have changed)."""
        if wifi.reachable(self.host):
            return True
        if time.time() - self._rescan_at < 30:
            return False
        self._rescan_at = time.time()
        hits = wifi.scan(timeout=0.3)
        if len(hits) == 1:
            self.host = hits[0]
            wifi.save(host=self.host)
            return True
        return False

    def set_choice(self, choice: str) -> None:
        """Automatic (default) or a fixed route he picked."""
        self.choice = choice if choice in ("auto", "usb", "wifi") else "auto"
        wifi.save(choice=self.choice)

    def set_connection(self, mode: str, host: str | None = None) -> None:
        """Switch between USB and Wi-Fi (closes the current session cleanly first)."""
        self.choice = "usb" if mode == "usb" else "wifi"     # his pick wins over Automatic
        wifi.save(choice=self.choice)
        self.shutdown()
        self.host = "usb" if mode == "usb" else (host or wifi.load().get("host") or "")
        if mode != "usb" and not self.host:
            raise ValueError("No camera IP yet: search for the camera first.")
        wifi.save(mode="usb" if self.host == "usb" else "wifi", **({} if self.host == "usb" else {"host": self.host}))
        self._fails, self._retry_at, self.fail_since, self.frame_error = 0, 0.0, 0.0, ""
        self._present_at = 0.0
        self.released = False

    def _mark_fail(self) -> None:
        if not self.fail_since:
            self.fail_since = time.time()

    def _mark_ok(self) -> None:
        self.fail_since = 0.0
        self.last_ok = time.time()

    def _present(self) -> bool | None:
        """Is the camera on the USB bus at all? (None = can't tell, e.g. Wi-Fi.)"""
        if time.time() - self._present_at > 2.0:
            try:
                if self.host == "usb":
                    self._present_cache = _camera_on_usb()
                else:
                    self._present_cache = wifi.reachable(self.host) if self.cam is None else True
            except Exception:  # noqa: BLE001
                self._present_cache = None
            self._present_at = time.time()
        return self._present_cache

    def health(self) -> dict:
        """ok | recovering | unresponsive | absent | released, with a message for the banner."""
        if self.released:
            return {"state": "released", "message": "Camera released (reload the page to reconnect)."}
        if not self.fail_since and self.cam is None and self.last_error:
            self.fail_since = time.time()          # Studio started with the camera off: that is a failure too, said plainly
        if not self.fail_since:
            if self.no_picture and self.cam is not None:
                return {"state": "ok", "message": "", "no_picture": self.no_picture}
            return {"state": "ok", "message": ""}
        down = time.time() - self.fail_since
        if down < self.WATCHDOG_S:
            return {"state": "recovering", "message": "Reconnecting to the camera…", "down_s": round(down, 1)}
        present = self._present()
        if present is False and self.host == "usb":
            try:
                from .ptpusb import sony_without_driver
                nodrv = sony_without_driver() if time.time() - getattr(self, "_nodrv_at", 0) > 10 else getattr(self, "_nodrv", [])
                self._nodrv, self._nodrv_at = nodrv, time.time()
            except Exception:  # noqa: BLE001
                nodrv = []
            mode = _usb_mode_of(nodrv[0].get("instance", "")) if nodrv else None
            if mode and not mode["control"]:
                return {"state": "absent", "down_s": round(down, 1), "usb_mode": True,
                        "message": f"The Sony {mode['model']} is on the cable in its {mode['usb_mode'] or 'file copy'} "
                                   "USB mode, which only copies files. On the camera set MENU › Network › PC Remote "
                                   "Function › PC Remote: On (USB connection: PC Remote); Studio connects by itself."}
            if nodrv:
                return {"state": "absent", "down_s": round(down, 1), "no_driver": True,
                        "message": f"A Sony camera ({nodrv[0]['name']}) is on the cable, but Windows has no driver for this camera "
                                   "that Studio can use yet. Studio does not install drivers. Wi-Fi may work instead."}
        if present is False and self.choice == "auto":
            return {"state": "absent", "down_s": round(down, 1),
                    "message": "Looking for the camera on the USB cable and on Wi-Fi. Check that it is on and that "
                               "PC Remote is on. Studio keeps trying both by itself."}
        if present is False:
            return {"state": "absent", "down_s": round(down, 1),
                    "message": "Camera not found on USB: switch it on and check the cable (USB Connection: PC Remote)."
                    if self.host == "usb" else
                    "Camera not reachable on Wi-Fi" + (f" ({self.host})" if self.host else "") + ": is it on, on your home Wi-Fi, with "
                    "PC Remote (Access Point) enabled? Studio keeps looking for it."}
        return {"state": "unresponsive", "down_s": round(down, 1),
                "message": "Camera not answering: switch it off and on. Studio reconnects by itself.",
                "detail": self.last_error}

    WATCHDOG_S = 5.0

    rescued: list = []
    waiting_note = ""

    def rescue_waiting(self, cam, when: str) -> list[str]:
        """An image the camera is holding for the PC (a body-button shot with 'save to PC', or a transfer that
        was interrupted) is fetched before the session closes or right after it opens, so a restart never
        drops it silently. Transfer only, never a shot."""
        try:
            pr = cam.refresh().get(SP_PENDING_IMAGES)
            v = pr.value if pr is not None and isinstance(pr.value, int) else 0
            if not v & 0x8000:
                return []
            files = cam.fetch_pending(timeout=10)
            saved = save_files(files)
            self.rescued = (self.rescued + saved)[-20:]
            self.waiting_note = (f"Saved {len(saved)} waiting image(s) from the camera {when}: " + ", ".join(saved)) if saved \
                else f"The camera had an image waiting {when}, but it could not be transferred."
            return saved
        except Exception as exc:  # noqa: BLE001
            self.waiting_note = f"The camera may have an image waiting ({when}); it could not be fetched: {exc}"
            return []

    def shutdown(self) -> None:
        """Close the PTP session cleanly (exit, console closed, Release button)."""
        self.released = True
        got = self.lock.acquire(timeout=3.0)
        try:
            if self.cam is not None:
                self.rescue_waiting(self.cam, "before closing")
                try:
                    self.cam.close()
                except Exception:  # noqa: BLE001
                    pass
                self.cam = None
        finally:
            if got:
                self.lock.release()

    def run(self, fn, keep_cache: bool = False):
        """Run fn(camera) under the lock; reconnect once if the link dropped.
        Anything but a read may change settings, so it drops the shared settings cache."""
        if not keep_cache:
            self._props = None
        self.waiters += 1
        t_wait = time.perf_counter()
        try:
            self.lock.acquire()
        finally:
            self.waiters -= 1
        t_hold = time.perf_counter()
        try:
            for attempt in (1, 2):
                try:
                    out = fn(self.camera())
                    self._mark_ok()
                    return out
                except Unsupported:
                    raise                  # the camera is fine; this driver just cannot do that (said in words)
                except (PTPError, OSError) as exc:
                    if not isinstance(exc, _Backoff):
                        self.last_error = str(exc)
                    self._mark_fail()
                    if self.cam is not None:
                        try:
                            self.cam.close()
                        except Exception:  # noqa: BLE001
                            pass
                        self.cam = None
                    if attempt == 2:
                        raise
        finally:
            self.lock.release()
            _perf_add("camera", getattr(fn, "__qualname__", "?").replace("build_app.<locals>.", "").replace("<lambda>", "λ")[:60],
                      (time.perf_counter() - t_hold) * 1000, (t_hold - t_wait) * 1000)

    def props(self, max_age: float = 1.5):
        """Camera properties, shared between callers for max_age seconds. Several
        panels poll the settings; without this each poll is a slow full read that
        freezes the live view for a moment."""
        if self._props is None or time.time() - self._props_at > max_age:
            props = self.run(lambda c: c.refresh(), keep_cache=True)
            self._props, self._props_at = props, time.time()
        return self._props

    MAX_FPS = 30          # asking faster only returns repeated frames and loads the camera

    def _pump(self) -> None:
        stamps: deque = deque(maxlen=60)
        last_crc, last_req = None, 0.0
        while True:
            if self.released or time.time() - self.last_view_req > 3:
                _real_sleep(0.1)
                continue
            if self.waiters:                     # let settings / presets / shutter go first
                _real_sleep(0.02)
                continue
            wait = 1 / self.MAX_FPS - (time.time() - last_req)
            if wait > 0:
                _real_sleep(wait)
            last_req = time.time()
            card = getattr(self, "card", None)
            if card is not None:                 # the capture card is the picture source (camera control stays)
                jpg, s = card.next_frame(getattr(self, "_card_seq", 0), timeout=0.5)
                if s == getattr(self, "_card_seq", 0) or jpg is None:
                    if card.stalled() and not getattr(self, "card_recording", False):
                        self.card_fallback("The capture card stopped sending a picture, so Studio shows the "
                                           "camera cable picture again.")
                    continue
                self._card_seq = s
            else:
                jpg = None
            try:
                if jpg is None:
                    jpg = self.run(lambda c: c.live_view(), keep_cache=True)
            except Unsupported as exc:            # e.g. a plain-PTP camera: connected, but it sends no picture
                self.frame_error = str(exc)
                self.no_picture = (self.picture_route or {}).get("say") or (str(exc) + " Settings and the shutter work.")
                self._find_picture_route()          # its webcam mode or a capture card instead (see liveroute.py)
                with self.frame_cond:
                    self.frame_cond.notify_all()
                _real_sleep(5.0)
                continue
            except Exception as exc:  # noqa: BLE001
                self.frame_error = str(exc) or exc.__class__.__name__
                with self.frame_cond:
                    self.frame_cond.notify_all()
                _real_sleep(1.0)
                continue
            crc = zlib.crc32(jpg)
            if crc == last_crc:              # camera hasn't made a new frame yet
                continue
            last_crc = crc
            now = time.time()
            stamps.append(now)
            while stamps and now - stamps[0] > 2.0:
                stamps.popleft()
            span = stamps[-1] - stamps[0]
            self.fps = (len(stamps) - 1) / span if len(stamps) > 2 and span > 0 else self.fps
            self.no_picture = ""
            with self.frame_cond:
                self.frame, self.frame_error = jpg, ""
                self.frame_t = now                       # when it arrived from the camera (for latency measuring)
                self.frame_seq += 1
                self.frame_cond.notify_all()
            rb = getattr(self, "reel", None)             # Playground › Day reel: the frame as received, memory only
            if rb is not None and rb.on:
                rb.pause(bool(self.recording))
                rb.feed(jpg, now)

    def _find_picture_route(self) -> None:
        """A camera without PTP live view: look (at most every 30 s, in the background) for its own webcam mode, which
        Studio then uses by itself, or a capture card, which is only offered. Opens nothing else."""
        if self._route_busy or time.time() - self._route_at < 30 or getattr(self, "card", None) is not None:
            return
        self._route_busy, self._route_at = True, time.time()

        def work():
            try:
                cam = self.cam
                if cam is None:
                    return
                devs = capture_card.list_devices()
                brand = getattr(cam, "brand", None) or "Sony"
                r = liveroute.choose(brand, camprofile.model_key(getattr(cam, "name", "")), False, devs.get("video") or [])
                self.picture_route = r
                if r["kind"] == "webcam" and devs.get("ffmpeg") and getattr(self, "card", None) is None:
                    src = capture_card.CardSource(r["device"])
                    src.start()
                    self._card_seq = 0
                    self.card, self.card_note, self.no_picture = src, r["say"], ""
                else:
                    self.no_picture = r["say"]
            except Exception as exc:  # noqa: BLE001 - never break the picture loop
                self.no_picture = self.no_picture or f"No live picture from this camera ({exc})."
            finally:
                self._route_busy = False
        threading.Thread(target=work, daemon=True, name="picture-route").start()

    def card_fallback(self, note: str) -> None:
        """Back to the camera cable picture (the card unplugged or silent), said in words."""
        card, self.card = getattr(self, "card", None), None
        self.card_note = note
        self._card_seq = 0
        if card is not None:
            threading.Thread(target=card.stop, daemon=True).start()

    def latest_frame(self, after: int, timeout: float = 2.0) -> tuple[bytes | None, int]:
        """Newest frame newer than `after` (waits up to `timeout` for one)."""
        self.last_view_req = time.time()
        self.released = False
        deadline = time.time() + timeout
        with self.frame_cond:
            while (self.frame is None or self.frame_seq <= after) and not self.frame_error:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.frame_cond.wait(remaining)
            return self.frame, self.frame_seq


PROP_LIVE_VIEW_QUALITY = SP_LIVE_VIEW_QUALITY   # Sony "Live View image quality" (1 standard, 2 high) on bodies that have it


def _usb_mode_of(instance: str) -> dict | None:
    """libgphoto2's entry for a Windows device id like USB\\VID_054C&PID_0DE3\\...: model and USB mode."""
    m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", instance.upper())
    if not m:
        return None
    from .drivers import registry
    return registry.lookup_usb(int(m[1], 16), int(m[2], 16))


def _camera_on_usb() -> bool:
    """Is a camera Studio can drive (any maker with a driver) on the USB bus? Errors are passed on (= can't tell)."""
    from .drivers import registry
    from .ptpusb import find_ptp_cameras
    if find_ptp_cameras(registry.SONY_VID):              # the tested camera first, as before
        return True
    vids = registry.controllable_vendors() - {registry.SONY_VID}
    return any(getattr(d, "idVendor", None) in vids for d in find_ptp_cameras(None))


def _best_live_view_quality(cam) -> None:
    """Ask for the sharper live-view stream when the camera offers the setting."""
    try:
        p = cam.refresh().get(PROP_LIVE_VIEW_QUALITY)
        if p is not None and p.writable and p.enabled and 2 in (p.choices or []) and p.value != 2:
            cam.set(PROP_LIVE_VIEW_QUALITY, 2)
    except Exception:  # noqa: BLE001 - optional nicety, never block connecting
        pass


def settings_view(props) -> list[dict]:
    """The Studio controls, with friendly labels, from the camera's properties."""
    out = []
    for name in CONTROLS:
        p = props.get(code_of(name))
        if p is None:
            continue
        choices = p.choices or ([p.value] if p.value is not None else [])
        if not p.choices and p.range:
            lo, hi, step = p.range
            choices = list(range(lo, hi + 1, step or 100))
        if name == "exposure_mode":
            choices = [c for c in choices if c in MODE] or choices
        out.append({"name": name, "value": p.value, "label": label(name, p.value),
                    "writable": p.writable,
                    "choices": [{"value": c, "label": label(name, c)} for c in choices]})
    return out


WB_SHIFT = {"wb_shift_ab": (presets.PROP_WB_SHIFT_AB, "WB shift amber/blue", "A", "B", 2),
            "wb_shift_gm": (presets.PROP_WB_SHIFT_GM, "WB shift green/magenta", "G", "M", 1)}
# Never offered to the Director: read-only status, mode class and recording/battery/card state.
DIRECTOR_EXCLUDE = {SP_SHOOT_CLASS, SP_REC_STATE, SP_BATTERY, SP_PENDING_IMAGES, SP_SHOTS_LEFT, SP_REC_TIME_LEFT, SP_STATUS, SP_FOCUS_INDICATION, SP_AF_LOCK, SP_MEDIA_STATUS}
VALUE_NAMES = {0x500B: {1: "Average", 2: "Centre-weighted", 3: "Multi", 4: "Spot", 0x8001: "Spot (S)",
                        0x8002: "Spot (L)", 0x8003: "Highlight", 0x8004: "Entire screen avg."}}


def wb_shift_label(name: str, v: int) -> str:
    _code, _f, pos, neg, step = WB_SHIFT[name]
    d = (v - 192) // step
    return "0" if d == 0 else f"{pos}{d}" if d > 0 else f"{neg}{-d}"


def director_code_of(name: str) -> int:
    if name.startswith("0x"):
        return int(name, 16)
    if name in WB_SHIFT:
        return WB_SHIFT[name][0]
    return code_of(name)


COLOUR_LOCKED = {SP_PICTURE_EFFECT, SP_DRO, SP_WB_SHIFT_AB, SP_WB_SHIFT_GM}       # picture effect, DRO, WB shifts: never by automation


def director_settings_view(props) -> list[dict]:
    """The main controls plus every other writable property the camera reports (friendly names
    where known), for the Director, undo and every automatic change. Colour-changing properties are
    left out (they stay neutral), and the formats are his (All settings › Camera › Format): only Studio's choice,
    his own picks and the value the camera has now are offered, never anything else."""
    out = settings_view(props)
    made = formats.choices_made()
    for s in out:
        if s["name"] == "picture_profile":
            keep = {0, 8, s["value"], made["photo"].get("picture_profile"), made["video"].get("picture_profile")}
            s["choices"] = [c for c in s["choices"] if c["value"] in keep] or s["choices"]
        if s["name"] == "still_format":
            keep = {2, s["value"], made["photo"].get("still_format")}
            s["choices"] = [c for c in s["choices"] if c["value"] in keep] or s["choices"]
    have = {code_of(s["name"]) for s in out} | COLOUR_LOCKED
    for name, (code, friendly, *_rest) in ({} if True else WB_SHIFT).items():
        p = props.get(code)
        have.add(code)
        if p is None or not (p.writable and p.enabled) or not (p.range or p.choices):
            continue
        choices = list(p.choices) or list(range(p.range[0], p.range[1] + 1, p.range[2] or 1))
        out.append({"name": name, "friendly": friendly, "value": p.value, "label": wb_shift_label(name, p.value),
                    "writable": True, "extra": True,
                    "choices": [{"value": c, "label": wb_shift_label(name, c)} for c in choices]})
    for code, p in sorted(props.items()):
        if code in have or code in DIRECTOR_EXCLUDE or not (p.writable and p.enabled):
            continue
        choices = list(p.choices or [])
        if not choices and p.range:
            lo, hi, step = p.range
            if step and (hi - lo) // step <= 64:
                choices = list(range(lo, hi + 1, step))
        if len(choices) < 2 or not isinstance(p.value, int):
            continue
        names = VALUE_NAMES.get(code, {})
        lab = lambda v, names=names: names.get(v, str(v))
        out.append({"name": f"0x{code:04X}", "friendly": KNOWN_PROPS.get(code, "unnamed property"),
                    "value": p.value, "label": lab(p.value), "writable": True, "extra": True,
                    "choices": [{"value": c, "label": lab(c)} for c in choices]})
    return out


def _num(props, code):
    p = props.get(code)
    return p.value if p is not None and isinstance(p.value, int) else None


def status_view(props) -> dict:
    """Battery, card space and the camera's own recording state."""
    batt = _num(props, PROP_BATTERY)
    rec = _num(props, PROP_REC_STATE)
    return {"battery": batt if batt is not None and batt >= 0 else None,
            "rec_time_left_s": _num(props, PROP_REC_TIME_LEFT) or None,
            "shots_left": _num(props, PROP_SHOTS_LEFT) or None,
            "recording": bool(rec) if rec is not None else None}


ACTIVE_BRAND = {"brand": "Sony"}          # maker of the connected camera (for RAW file endings)


def save_files(files) -> list[str]:
    """Save transferred photos (JPEG / RAW) into the captures folder; returns file names."""
    from .drivers.base import file_ext
    CAPTURE_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved = []
    for i, (fmt, data) in enumerate(files):
        ext = file_ext(data, ACTIVE_BRAND["brand"])
        path = CAPTURE_DIR / f"{stamp}_{i + 1}{ext}"
        n = 2
        while path.exists():
            path = CAPTURE_DIR / f"{stamp}_{i + 1}-{n}{ext}"
            n += 1
        path.write_bytes(data)
        saved.append(path.name)
    return saved


def _max_iso(body: dict) -> int | None:
    try:
        v = int(body.get("max_iso") or 0)
    except (TypeError, ValueError):
        return None
    return v if 100 <= v <= 51200 else None


def _local_pick(mode: str, level: float) -> dict:
    """Built-in autopilot (no API key needed): pick from the measured light."""
    if mode == "video":
        key = "lowlight_video" if level < 60 else "cinematic"
    else:
        key = "night" if level < 35 else "lowlight" if level < 80 else "portrait"
    why = {"lowlight_video": "The room is dark, so this keeps the picture usable with the least noise.",
           "cinematic": "Balanced light: 1/50 shutter, wide aperture, normal colour for a filmic look.",
           "night": "Very dark scene: widest aperture and the slowest safe handheld shutter.",
           "lowlight": "Dim light: wide aperture and a slower shutter keep the ISO (grain) down.",
           "portrait": "Good light for a portrait: wide aperture for background blur, fast enough shutter."}[key]
    return {"preset": key, "look": None, "reason": why,
            "tip": "More light on the subject (a window or lamp) is the biggest quality upgrade." if level < 80 else ""}


def neutral_preset(c, key: str, look=None) -> dict:
    """Legacy preset for automation sequences, made neutral: no look, then RAW / S-Log3 and WB shift centred."""
    rep = presets.apply_preset(c, key, None, label=label)
    video = presets.PRESETS[key].section == "video"
    props = c.refresh()
    m = "video" if video else "photo"
    prof = camprofile.for_model(getattr(c, "name", None))
    pp = formats.wanted(m, "picture_profile", props, prof, label)[0]
    pp = 0 if pp is None and not video else pp
    sf = None if video else formats.wanted("photo", "still_format", props, prof, label)[0]
    want = ([(presets.PROP_PICTURE_PROFILE, pp)] if pp is not None else []) + ([(SP_STILL_FORMAT, sf)] if sf else []) + \
        [(presets.PROP_WB_SHIFT_AB, 192), (presets.PROP_WB_SHIFT_GM, 192)]
    for code, v in want:
        p = props.get(code)
        if p is not None and p.writable and (v in (p.choices or []) or p.range) and p.value != v:
            try:
                c.set(code, v)
            except Exception:  # noqa: BLE001
                pass
    return rep


def build_app(studio: Studio, start_auto: bool = False) -> Starlette:
    auto = automation.Automation(studio, save_files, lambda c, key, look: neutral_preset(c, key))
    follower = followlight.LightFollower(studio)
    changelog = director.ChangeLog(studio, director_settings_view, director_code_of,
                                   CONTROLS + list(WB_SHIFT))
    dirx = director.Director(studio, changelog, follower, label)

    def err(exc: Exception, code: int = 503):
        return JSONResponse({"error": str(exc)}, status_code=code)

    def live_frame() -> bytes:
        frame, _ = studio.latest_frame(-1, timeout=2.0)
        return frame if frame is not None else studio.run(lambda c: c.live_view(), keep_cache=True)

    def _refs(body: dict) -> list[str]:
        refs = [str(r).split(",", 1)[-1] for r in (body.get("refs") or [])[:4] if r]
        if any(len(r) > 6_000_000 for r in refs):
            raise ValueError("Reference image too large")
        return refs

    # -- DIRECTOR: Claude in control ----------------------------------------
    async def director_start(request: Request):
        body = await request.json()
        try:
            dirx.start(str(body.get("goal", ""))[:1000], "video" if body.get("mode") == "video" else "photo")
            return JSONResponse({"ok": True})
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def director_stop(_: Request):
        dirx.stop()
        return JSONResponse({"ok": True})

    def director_status(_: Request):
        log = [{k: v for k, v in e.items() if k != "undo"} | {"undoable": bool(e["undo"]) and not e["undone"]}
               for e in reversed(changelog.entries[-120:])]
        return JSONResponse({**dirx.status, "log": log, "version": changelog.version,
                             "assistant_ready": assistant.has_key()})

    async def director_undo(request: Request):
        body = await request.json()
        if body.get("all"):
            ids = [e["id"] for e in changelog.entries]
        elif body.get("run") is not None:
            ids = [e["id"] for e in changelog.entries if e.get("run") == body["run"]]
        else:
            ids = [int(i) for i in body.get("ids", [])]
        if dirx.status["running"]:
            return JSONResponse({"error": "Stop the director before undoing."}, status_code=400)
        failed = await anyio.to_thread.run_sync(lambda: changelog.undo(ids))
        return JSONResponse({"ok": not failed, "failed": failed})

    async def changes_apply(request: Request):
        body = await request.json()
        changes = [{"name": c.get("name"), "value": c.get("value")} for c in body.get("changes", [])]
        source = str(body.get("source", "lighting"))[:20]

        def work():
            follower.pause(10)
            return changelog.apply(changes, source, str(body.get("reason", ""))[:300])
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- SHOOT FOR POST (default): S-Log3 video / RAW+JPEG photos; looks are monitor preview only --
    prefs_file = setups.BASE / "prefs.json"

    def prefs() -> dict:
        try:
            p = {"post": True, **json.loads(prefs_file.read_text(encoding="utf-8"))}
            p.pop("quick_draft", None)            # the removed automatic draft: an old saved value is ignored
            return p
        except (OSError, ValueError):
            return {"post": True}

    def save_prefs(**kw) -> dict:
        p = {**prefs(), **kw}
        prefs_file.parent.mkdir(parents=True, exist_ok=True)
        prefs_file.write_text(json.dumps(p), encoding="utf-8")
        return p

    def _migrate_photo_log() -> None:
        """The old "Photo preview like video" switch is now the Photo picture profile row: if it was on, his photo
        choice becomes the log profile (once), and the switch is cleared. Off: nothing changes."""
        try:
            if not prefs().get("photo_log") or studio.cam is None:
                return
            lp = formats._log_pp(camprofile.of(studio))
            if lp is not None and formats.chosen("photo", "picture_profile") is None:
                formats.set_choice("photo", "picture_profile", lp)
            save_prefs(photo_log=False)
        except Exception:  # noqa: BLE001
            pass

    def post_goal() -> str:
        """The format rule for Claude's prompts: the format he chose (Studio's choice by default), never changed."""
        try:
            props = studio.props(max_age=5)
            prof = camprofile.of(studio)
            m = "video" if (props.get(PROPS["exposure_mode"]) and props[PROPS["exposure_mode"]].value in autofix.MOVIE_MODES) else "photo"
            fmt = formats.summary(m, props, prof, label)["name"] or ("RAW + JPEG" if m == "photo" else "the camera's video format")
        except Exception:  # noqa: BLE001
            fmt = "the format set in Studio"
        return (f" Neutral capture in the file format he chose ({fmt}); never change the file format or picture profile. "
                "No looks, no colour settings, WB shift centred. Noise guard: ISO at most 6400 video / 3200 photo; "
                "if the light is not enough, say what light to add instead of pushing ISO.")

    def apply_post(mode: str) -> dict:
        """Put the camera in this mode's format (his choice, else Studio's choice), logged and undoable."""
        props = studio.props(max_age=0)
        vals = {s["name"]: s for s in settings_view(props)}
        prof = camprofile.of(studio)
        photo_log = bool(prefs().get("photo_log", False))
        ch = []
        keys = ("picture_profile",) if mode == "video" else ("still_format",) + \
            (("picture_profile",) if formats.chosen("photo", "picture_profile") is not None else ())
        for key in keys:
            v = formats.wanted(mode, key, props, prof, label, photo_log)[0]
            s = vals.get(key)
            if v is not None and s and any(c["value"] == v for c in s["choices"]) and s["value"] != v:
                ch.append({"name": key, "value": v})
        if not ch:
            return {"applied": [], "failed": []}
        return changelog.apply(ch, "post", "Format for " + mode + ": " +
                               ", ".join(formats.value_name(c["name"], c["value"], prof, label) for c in ch))

    mode_lock = threading.RLock()                      # one Photo/Video setup at a time: never two writing at once
    mode_req = {"n": 0}                                 # the newest request wins; an older one waiting is dropped

    def mode_setup(m: str, who: str = "You", event: str | None = None) -> dict:
        with mode_lock:
            return _mode_setup(m, who, event)

    def _mode_setup(m: str, who: str = "You", event: str | None = None) -> dict:
        """He switched Photo <-> Video: one complete fresh setup for the new mode, like the first one.
        The camera keeps separate exposure / focus memories per mode, so nothing is carried over: format,
        neutral colour, shutter from frame rate + mains, measured exposure for this format, focus acquired
        once and locked, new brightness reference. Built-in rules (no Claude call)."""
        autof.studio_busy(120)
        autof._event(event or f"{who} switched to {m}", kind="you" if who == "You" else "setup")
        autof.phase, autof.state = "deciding", f"Setting up for {m}…"
        try:
            time.sleep(1.0)                                     # let the mode change and the live view settle
            key = (autof.decision or {}).get("shot") or ""
            e, v, _x = run_profile(key, m)
            autofix._mark_setup_time()                          # a restart of Studio does not set up again
        except Exception as exc:  # noqa: BLE001
            autof.phase, autof.state = "locked", f"Setup for {m} failed: {exc}"
            raise
        autof.baseline = {"ire": None, "shot": key or None, "subject": (autof.baseline or {}).get("subject"),
                          "cast": None, "t": time.time(), "mode": m, "sharp": None, "size": None}
        autof.history = []
        autof.proposal = None
        autof._off_since.clear()
        autof._quiet_until = time.time() + 6
        autof._studio_until = time.time() + 6
        autof.known = {}
        warn = [i["text"] for i in v["items"] if i["status"] != "ok"]
        autof._event(f"Studio set up for {m}" + (": " + "; ".join(warn)[:160] if warn else ""), e, kind="setup")
        autof.decision = {**(autof.decision or {}), "verdict": v}
        autof.phase, autof.state = "locked", f"Locked: {m}"
        try:
            autofix._save_fp(autof.values())                # these are now "as Studio set it" (for a reconnect)
        except Exception:  # noqa: BLE001
            pass
        studio.pp_note = ""
        prof = camprofile.of(studio)
        if m == "video" and prof.get("builtin") and studio.frame:
            try:
                ppv = studio.props(max_age=2).get(presets.PROP_PICTURE_PROFILE)
                if ppv is not None and ppv.value == prof.get("log_pp") and not camprofile.looks_like_log(studio.frame)["log_like"]:
                    studio.pp_note = (f"PP{ppv.value} doesn't look like S-Log3 on this camera any more (was it edited on the "
                                      "camera?). Studio changed nothing.")
            except Exception:  # noqa: BLE001
                pass
        return v

    async def post_view(request: Request):
        if request.method == "POST":
            body = await request.json()
            p = save_prefs(**{k: bool(body[k]) for k in ("post", "photo_log") if k in body},
                           **{k: int(body[k]) for k in ("mains_hz", "raw_people_iso", "iso_ceiling_photo", "iso_ceiling_video") if k in body and int(body[k]) > 0})
            if body.get("mode"):
                m = "video" if body["mode"] == "video" else "photo"
                mode_req["n"] += 1
                mine = mode_req["n"]

                def work():
                    with mode_lock:                      # wait for a setup that is still running, never overlap it
                        if mine != mode_req["n"]:
                            return None                  # he already switched again: this one is out of date
                        r = apply_post(m) if body.get("post", prefs().get("post")) else {"applied": [], "failed": []}
                        v = mode_setup(m) if body.get("setup", True) else None
                        return r, v
                out = await anyio.to_thread.run_sync(work)
                if out is None:
                    return JSONResponse({**p, "applied": [], "failed": [], "superseded": True, **autof.status()})
                r, v = out
                if v is not None:
                    return JSONResponse({**p, "applied": r["applied"], "failed": r["failed"], "verdict": v, **autof.status()})
                return JSONResponse({**p, "applied": r["applied"], "failed": r["failed"]})
            return JSONResponse(p)
        return JSONResponse(prefs())

    def run_profile(key: str, mode: str, moving: bool = False) -> tuple[dict, dict, dict]:
        """Run a capture profile; never while the lens is zooming (its exposure loop would chase the moving picture)."""
        end = time.time() + 8.0
        while (zoom_state["moving"] or zoom_state["busy"]) and time.time() < end:
            time.sleep(0.1)
        studio.setup_active = getattr(studio, "setup_active", 0) + 1
        try:
            return _run_profile(key, mode, moving)
        finally:
            studio.setup_active -= 1

    def _run_profile(key: str, mode: str, moving: bool = False) -> tuple[dict, dict, dict]:
        """Run a capture profile (neutral, RAW / S-Log3, exposure by measurement). Returns (entry, verdict, exposure)."""
        key = key if key in capture.PROFILES else ("interview" if mode == "video" else "portrait")
        prof = capture.PROFILES[key]
        _migrate_photo_log()
        before = changelog.snapshot()
        follower.pause(60)
        try:
            run = capture.CaptureRun(studio, lambda code, v: studio.run(lambda c: c.set(code, v)),
                                     lambda: studio.props(max_age=0), label, wb_lock=prefs().get("wb_kelvin"))
            run.mains_hz = int(prefs().get("mains_hz", 50))
            run.photo_log = bool(prefs().get("photo_log", False))   # photo: normal picture unless "Match video" is on
            run.cap_override = int(prefs().get("iso_ceiling_" + mode, capture.ISO_CEILING[mode]))   # exposure first
            notes = run.formats(mode)
            time.sleep(0.6)
            run.neutral_colour()
            wb_locked = run.white_balance()
            run.lens_and_shutter(prof, mode)
            mf = measure.analyze(studio.frame) if studio.frame else {}
            face_box = (mf.get("face") or {}).get("box") if (mf.get("face") or {}).get("method") == "face detection" else None
            run.focus(prof, mode, face_box, moving)
            p_now = studio.props(max_age=0)
            ppv = p_now.get(presets.PROP_PICTURE_PROFILE)
            cprof = camprofile.of(studio)
            run.curve = camprofile.log_kind(cprof, ppv.value) if ppv else None      # S-Log3 / S-Log2 targets
            sfv = p_now.get(SP_STILL_FORMAT)
            run.jpeg_only = mode == "photo" and sfv is not None and sfv.value == 3   # no RAW: protect highlights
            exp = run.expose(prof, mode, bool(ppv and camprofile.is_log(cprof, ppv.value)))   # measure on the curve in use
            frame = live_frame()
            a = coach.analyze(frame, {s["name"]: s["value"] for s in settings_view(studio.props(max_age=0))},
                              status_view(studio.props()))
            run.autofocus(prof, face_box)
            time.sleep(0.8)
            a = coach.analyze(live_frame(), {s["name"]: s["value"] for s in settings_view(studio.props(max_age=0))},
                              status_view(studio.props()))
            focus_ok = next((c["status"] == "ok" for c in a["checks"] if c["key"] == "focus"), None)
            mismatch = run.readback()
        finally:
            follower.pause(2)
        props = studio.props(max_age=0)
        fps = capture.fps_of(label("movie_setting", props[SP_MOVIE_SETTING].value)) if props.get(SP_MOVIE_SETTING) else 25
        cprof = camprofile.of(studio)
        want = {}
        for k in (("picture_profile",) if mode == "video" else ("still_format",)):
            wv = formats.wanted(mode, k, props, cprof, label, bool(prefs().get("photo_log", False)))[0]
            if wv is not None:
                want[k] = wv
        if mode == "video" and getattr(run, "flicker_safe", True) is False:
            notes = notes + [f"At {fps}p no shutter is free of flicker under {run.mains_hz} Hz lights: daylight or "
                             "flicker-free lamps, or a lower frame rate"]
        v = capture.verdict(mode, exp, props, wb_locked, focus_ok, notes, mismatch, label, shot=key, profile=cprof,
                            gear=setups.get_gear(), fps=fps, iso_cap=getattr(run, "cap_override", None), want=want)
        after = changelog.snapshot()
        undo = {n: val for n, val in before.items() if after.get(n) != val}
        fname = formats.summary(mode, props, cprof, label)["name"] or mode
        e = changelog.add("capture", "preset", f"Clean capture: {prof['name']} ({fname})", v["todo"], undo)
        last_verdict.update(v, mode=mode, profile=key, t=time.time())
        focus_state.update(text=getattr(run, "focus_text", ""), point=None if getattr(run, "track", False) else getattr(run, "target", None),
                           tracking=bool(getattr(run, "track", False)), t=time.time())
        try:                                    # what Studio chose ("Studio's choice" in the pickers)
            vals = _values()
            autof.chosen = {k: vals.get(k) for k in ("aperture", "shutter_speed", "iso", "white_balance", "color_temp", "exposure_mode")}
        except Exception:  # noqa: BLE001
            pass
        return e, v, exp

    last_verdict: dict = {}
    focus_state: dict = {"text": "", "point": None, "tracking": False}

    def _patch_cast(frame: bytes) -> tuple[float, float]:
        """(R-B)/(R+B) and brightness (IRE) of the centre patch (where the grey card is held)."""
        from PIL import Image as _I, ImageStat as _S
        im = _I.open(__import__("io").BytesIO(frame)).convert("RGB")
        w, h = im.size
        r, g, b = _S.Stat(im.crop((int(w * .38), int(h * .32), int(w * .62), int(h * .68)))).mean
        return (r - b) / max(r + b, 1), (0.2126 * r + 0.7152 * g + 0.0722 * b) * 100 / 255

    async def wb_measure(_: Request):
        """Measure white balance on a grey card / white paper in the centre and lock it (Kelvin)."""
        def work():
            follower.pause(30)
            cast, ire = _patch_cast(live_frame())
            if not 15 <= ire <= 96:
                raise ValueError("Hold a grey card or white paper in the middle of the picture, filling the box, "
                                 "evenly lit (it's too " + ("dark" if ire < 15 else "bright") + ").")
            props = studio.props(max_age=0)
            if WB[32786] and 32786 not in props[PROPS["white_balance"]].choices:
                raise ValueError("This camera mode doesn't offer Kelvin white balance.")
            studio.run(lambda c: c.set("white_balance", 32786))
            ks = capture._choices(studio.props(max_age=0), SP_COLOR_TEMP)
            if not ks:
                raise ValueError("No colour-temperature values offered.")
            lo, hi, best = 0, len(ks) - 1, None
            for _ in range(8):
                if lo > hi:
                    break
                mid = (lo + hi) // 2
                studio.run(lambda c, k=ks[mid]: c.set(SP_COLOR_TEMP, k))
                seq = studio.frame_seq
                time.sleep(0.7)
                f, _s = studio.latest_frame(seq + 2, timeout=3)
                d, _i = _patch_cast(f or live_frame())
                if best is None or abs(d) < abs(best[1]):
                    best = (ks[mid], d)
                if abs(d) < 0.008:
                    break
                if d < 0:                     # too blue -> a higher Kelvin setting warms the picture
                    lo = mid + 1
                else:
                    hi = mid - 1
            studio.run(lambda c: c.set(SP_COLOR_TEMP, best[0]))
            autof.baseline.update(ire=None, cast=None)
            save_prefs(wb_kelvin=best[0], wb_at=time.time())
            changelog.add("you", "change", f"White balance locked: {best[0]} K (measured on the grey card)",
                          f"residual cast {best[1]:+.3f}")
            return {"kelvin": best[0], "residual": round(best[1], 3), "ok": abs(best[1]) < 0.03}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def _converge_kelvin(cast_of_frame) -> tuple[int, float]:
        """Binary-search the Kelvin setting until cast_of_frame(frame) ~ 0 (read back on the live frame)."""
        studio.run(lambda c: c.set("white_balance", 32786))
        ks = capture._choices(studio.props(max_age=0), SP_COLOR_TEMP)
        if not ks:
            raise ValueError("No colour-temperature values offered in this mode.")
        lo, hi, best = 0, len(ks) - 1, None
        for _ in range(8):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            studio.run(lambda c, k=ks[mid]: c.set(SP_COLOR_TEMP, k))
            seq = studio.frame_seq
            time.sleep(0.7)
            f, _s = studio.latest_frame(seq + 2, timeout=3)
            d = cast_of_frame(f or live_frame())
            if d is None:
                break
            if best is None or abs(d) < abs(best[1]):
                best = (ks[mid], d)
            if abs(d) < 0.008:
                break
            if d < 0:
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            raise ValueError("Couldn't measure the neutral areas.")
        studio.run(lambda c: c.set(SP_COLOR_TEMP, best[0]))
        return best

    WB_SYSTEM = ("You look at a camera frame and point out surfaces that are truly neutral in colour in reality "
                 "(white or grey walls, paper, white fabric, grey card, whites of the eyes), not coloured objects, "
                 "and name the light sources (window daylight, tungsten lamp, LED panel, screen, mixed).")

    async def wb_auto(_: Request):
        """White balance without a card: neutral regions in the frame (local, optionally confirmed by Claude),
        converge on the Kelvin by read-back, lock it. Returns regions + confidence."""
        def work():
            follower.pause(40)
            frame = live_frame()
            props = studio.props(max_age=0)
            before_wb = {"white_balance": props[PROPS["white_balance"]].value,
                         "color_temp": props[SP_COLOR_TEMP].value if props.get(SP_COLOR_TEMP) else None}
            pp = props.get(presets.PROP_PICTURE_PROFILE)
            log = bool(pp and camprofile.is_log(camprofile.of(studio), pp.value))
            to709 = measure._to_709 if log else None
            mf = measure.analyze(frame, log=log)
            face = (mf.get("face") or {}).get("box") if (mf.get("face") or {}).get("method") == "face detection" else None
            found = wbauto.find_neutrals(frame, to709, face)
            lights, by = [], "local"
            if assistant.has_key():
                import base64 as _b64
                schema = {"type": "object", "additionalProperties": False, "required": ["regions", "lights", "confidence"],
                          "properties": {"regions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["x", "y", "w", "h", "what"],
                                         "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"},
                                                        "h": {"type": "number"}, "what": {"type": "string"}}}},
                                         "lights": {"type": "array", "items": {"type": "string"}},
                                         "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}
                body = {"model": assistant.MODEL, "max_tokens": 3000, "system": WB_SYSTEM,
                        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}, "fallbacks": "default",
                        "messages": [{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64.standard_b64encode(frame).decode()}},
                            {"type": "text", "text": "Give the neutral regions as boxes (x, y, w, h from 0 to 1, top-left origin)."
                             + (" The image is S-Log3 (flat); judge objects, not the flat colour." if log else "")}]}]}
                try:
                    with assistant.feature("white balance"):
                        out = assistant._json_answer(assistant.call(body, assistant._key()))
                    regs = [[min(max(r["x"], 0), 1), min(max(r["y"], 0), 1), min(max(r["w"], 0.02), 1), min(max(r["h"], 0.02), 1)]
                            for r in out.get("regions", [])][:6]
                    lights = out.get("lights", [])[:4]
                    if regs and wbauto.regions_cast(frame, regs, to709) is not None:
                        found = {**found, "regions": regs, "confidence": out.get("confidence", found["confidence"]),
                                 "what": [r["what"] for r in out.get("regions", [])][:6]}
                        by = "claude"
                        if any("mix" in l.lower() for l in lights) or len({l.split()[0].lower() for l in lights}) > 1:
                            found["mixed"] = True
                except Exception:  # noqa: BLE001 - the local search does the job alone
                    pass
            if not found["regions"]:
                raise ValueError(found["reason"] + " Hold a grey card or white paper up and use “Measure card”.")
            k, resid = _converge_kelvin(lambda f: wbauto.regions_cast(f, found["regions"], to709))
            autof.baseline.update(ire=None, cast=None)          # new WB changes the picture: new reference, no false "light changed"
            conf = found["confidence"] if abs(resid) < 0.03 else "low"
            save_prefs(wb_kelvin=k, wb_at=time.time(), wb_method=f"auto ({by}, {conf})")
            what = ", ".join(found.get("what", [])[:3]) or f"{len(found['regions'])} neutral area(s)"
            changelog.add("you", "change", f"White balance locked: {k} K from {what} ({conf} confidence)",
                          "Video bakes this in (S-Log3)" if log else "RAW: still adjustable in post",
                          {k2: v2 for k2, v2 in before_wb.items() if v2 is not None})
            return {"kelvin": k, "confidence": conf, "regions": found["regions"], "what": found.get("what", []),
                    "lights": lights, "mixed": found["mixed"], "reason": found["reason"], "by": by,
                    "residual": round(resid, 3), "video_baked": log}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def wb_clear(_: Request):
        save_prefs(wb_kelvin=None)
        return JSONResponse({"ok": True})

    def _with(name, fn):
        with assistant.feature(name):
            return fn()

    async def usage_view(request: Request):
        """Claude token counts (today, per feature, per shoot) + the "Claude on connect" setting. Tokens only."""
        if request.method == "POST":
            body = await request.json()
            if "claude_on_connect" in body:
                save_prefs(claude_on_connect=bool(body["claude_on_connect"]))
        return JSONResponse({**assistant.usage.today(), "claude_on_connect": prefs().get("claude_on_connect", True)})

    ui_log_file = setups.BASE / "ui_usage.json"

    async def ui_log(request: Request):
        """Counts of which UI controls are used (control ids only, no content), to decide what belongs on screen."""
        if request.method == "POST":
            a = str((await request.json()).get("a", ""))[:40]
            if a:
                try:
                    d = json.loads(ui_log_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    d = {}
                day = d.setdefault(time.strftime("%Y-%m-%d"), {})
                day[a] = day.get(a, 0) + 1
                ui_log_file.parent.mkdir(parents=True, exist_ok=True)
                ui_log_file.write_text(json.dumps(d), encoding="utf-8")
            return JSONResponse({"ok": True})
        try:
            return JSONResponse(json.loads(ui_log_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return JSONResponse({})

    def verdict_view(_: Request):
        p = prefs()
        return JSONResponse({**last_verdict, "wb_kelvin": p.get("wb_kelvin"), "profiles": capture.catalog()})
    dirx.run_profile = run_profile

    def enforce_format(mode: str) -> list[str]:
        """The format he chose for this mode (Studio's choice by default: RAW + JPEG photos, S-Log3 video) and neutral
        colour: set back if the camera is on anything else."""
        changed = []
        run = capture.CaptureRun(studio, lambda code, v: (studio.run(lambda c: c.set(code, v)), changed.append((code, v))),
                                 lambda: studio.props(max_age=0), label)
        run.photo_log = bool(prefs().get("photo_log", False))
        run.formats(mode)
        run.neutral_colour()
        prof = camprofile.of(studio)
        by_code = {c: k for k, c in formats.CODES.items()}
        names = {capture.presets.PROP_WB_SHIFT_AB: "WB shift → neutral",
                 capture.presets.PROP_WB_SHIFT_GM: "WB shift → neutral", SP_JPEG_QUALITY: "JPEG quality → best",
                 SP_IMAGE_SIZE: "image size → L", capture.PROP_DRO: "DRO → off", capture.PROP_PICTURE_EFFECT: "picture effect → off"}
        titles = {"still_format": "file format", "jpeg_quality": "JPEG quality", "image_size": "image size",
                  "aspect": "aspect ratio", "picture_profile": "picture profile",
                  "movie_format": "video file", "movie_setting": "frame rate and quality"}
        out = []
        for code, v in dict.fromkeys(changed):
            if code == PROPS["exposure_mode"]:
                continue
            k = by_code.get(code)
            out.append(f"{titles[k]} → {formats.value_name(k, v, prof, label)}" if k else names.get(code, hex(code)))
        out = list(dict.fromkeys(out))
        if out:
            autof._event("Set back for a pro capture: " + ", ".join(out), kind="setup")
        return out

    def builtin_setup(mode: str, preset: str | None = None, look: str | None = None):
        """Clean capture (no Claude): a capture profile. Kept under this name for the callers.
        Returns (entry, report, key). Looks are never applied to the camera."""
        key = preset if preset in capture.PROFILES else {"interview": "interview", "portrait": "portrait",
                                                          "product": "product", "broll": "product",
                                                          "landscape": "daylight", "vlog": "daylight",
                                                          "lowlight": "lowlight", "lowlight_video": "lowlight",
                                                          "night": "lowlight"}.get(preset or "", None)
        e, v, exp = run_profile(key or "", mode)
        return e, {"warning": v["todo"], "tips": [i["todo"] for i in v["items"] if i["todo"]][:3], "verdict": v}, \
            (key or ("interview" if mode == "video" else "portrait"))

    def _old_builtin_setup(mode: str, preset: str | None = None, look: str | None = None):
        """The built-in autopilot (no Claude), logged as one undoable entry. Returns (entry, report, key).
        In 'shoot for post' mode video uses the LOG preset and no look is baked in."""
        frame, _ = studio.latest_frame(-1, timeout=2)
        level = presets.brightness(frame)[0] if frame else 100
        if prefs()["post"]:
            look = None
            if mode == "video":
                preset = "log"
        key = preset if preset in presets.PRESETS and presets.PRESETS[preset].section == mode else _local_pick(mode, level)["preset"]
        before = changelog.snapshot()
        follower.pause(30)
        try:
            rep = studio.run(lambda c: presets.apply_preset(c, key, look, label=label))
        finally:
            follower.pause(2)
        if prefs()["post"]:
            apply_post(mode)
        after = changelog.snapshot()
        undo = {n: v for n, v in before.items() if after.get(n) != v}
        e = changelog.add("auto", "preset", f"Built-in setup: {presets.PRESETS[key].name}"
                          + (f" + {presets.LOOKS[look]['name']}" if look else ""), rep.get("warning") or "", undo)
        return e, rep, key

    def _values() -> dict:
        return {s["name"]: s["value"] for s in settings_view(studio.props(max_age=3))}

    def _analyze(frame: bytes) -> dict:
        props = studio.props(max_age=3)
        return coach.analyze(frame, {s["name"]: s["value"] for s in settings_view(props)}, status_view(props))

    # -- OPERATOR: scene analysis -> decision -> lock & hold -> proposals ----------
    def _scene(frame: bytes) -> dict:
        props = studio.props(max_age=6)
        pp = props.get(presets.PROP_PICTURE_PROFILE)
        return scene.analyze(frame, log=bool(pp and camprofile.is_log(camprofile.of(studio), pp.value)), props=props,
                             history=getattr(autof, "history", None))

    DECIDE_SYSTEM = (
        "You are a cinematographer deciding how to shoot what the camera sees, for a professional who grades "
        "it himself, in the format he chose (given below), always neutral. From the frame and the measurements choose the "
        "shot type, write at most four short cinematographer's notes on why (subject, what must be sharp, the "
        "light, depth of field, shutter), and at most three concrete physical directions for what the camera "
        "cannot fix (move the light or the subject, turn away from the window, add fill on the shadow side, "
        "more distance to the background, raise the camera, use an ND). Plain words, no camera-menu jargon.")

    def _claude_decide(frame: bytes, sc: dict, mode: str) -> dict | None:
        import base64 as _b64
        schema = {"type": "object", "additionalProperties": False, "required": ["shot", "why", "directions"],
                  "properties": {"shot": {"type": "string", "enum": capture.ORDER},
                                 "why": {"type": "array", "items": {"type": "string"}},
                                 "directions": {"type": "array", "items": {"type": "string"}}}}
        meas = {k: v for k, v in sc.items() if k not in ("override",)}
        body = {"model": assistant.MODEL, "max_tokens": 4000, "system": DECIDE_SYSTEM,
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}, "fallbacks": "default",
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64.standard_b64encode(frame).decode()}},
                    {"type": "text", "text": f"Mode: {mode}. Measurements: {json.dumps(meas)}"}]}]}
        with assistant.feature("operator decision"):
            out = assistant._json_answer(assistant.call(body, assistant._key()))
        return out if out.get("shot") in capture.PROFILES else None

    def _consistency(mode: str, v: dict) -> str:
        prev = [t for t in importer.takes(handoff.current()) if t.get("measure")] if handoff.current() else []
        prev = [t for t in prev if (t["kind"] == "video") == (mode == "video")]
        if not prev:
            return ""
        p = prev[-1]["measure"]
        now = {s["name"]: s["value"] for s in settings_view(studio.props(max_age=0))}
        diffs = [n.replace("_", " ") for n in ("shutter_speed", "white_balance", "color_temp", "movie_setting")
                 if n in p.get("values", {}) and p["values"][n] != now.get(n)]
        return (f"Differs from take {prev[-1]['id']} in {', '.join(diffs)}: check it's on purpose."
                if diffs else f"Consistent with take {prev[-1]['id']} (shutter, white balance, frame rate).")

    def _decide(frame: bytes, sc: dict, mode: str, use_claude: bool) -> dict:
        rule = scene.decide(sc, mode) if sc else {"shot": "interview" if mode == "video" else "portrait", "why": [], "directions": []}
        shot, why, dirs, by = sc.get("override") or rule["shot"], rule["why"], rule["directions"], "rules"
        if use_claude and not sc.get("override"):
            try:
                c = _claude_decide(frame, sc, mode)
                if c:
                    shot, why, by = c["shot"], c["why"][:4], "claude"
                    dirs = [{"key": "claude", "light": None, "text": t} for t in c["directions"][:3]] or dirs
            except Exception:  # noqa: BLE001 - the rules do the job without Claude
                pass
        e, v, exp = run_profile(shot, mode)          # tracking only when he chooses it (Track my eyes)
        cons = _consistency(mode, v)
        return {"shot": shot, "shot_name": scene.SHOT_NAMES.get(shot, shot), "why": (why + ([cons] if cons else []))[:5],
                "directions": dirs, "entry": e, "verdict": v, "by": by}

    def _apply_proposal(p: dict) -> list:
        if p["kind"] == "focus":
            refocus_target()
            return ["focus"]
        if p["kind"] != "light":
            return []                                    # colour: needs the grey card (the owner does it)
        ch = _light_plan(p)
        if not ch:
            return []
        r = changelog.apply(ch, "operator", p["text"])
        return r["applied"]

    def _light_plan_text(p: dict) -> str:
        """What Apply would set, in his words, shown on the card before he presses (e.g. 'f/6.3 · ISO 100')."""
        try:
            return " · ".join(("ISO " if c["name"] == "iso" else "") + label(c["name"], c["value"]) for c in _light_plan(p))
        except Exception:  # noqa: BLE001
            return ""

    def _light_plan(p: dict) -> list:
        stops = float(p.get("stops", 0))
        mode = autof.mode(_values())
        props = studio.props(max_age=0)
        aps = sorted(capture._choices(props, PROPS["aperture"]))
        isos = sorted(c for c in props[PROPS["iso"]].choices if 0 < c < 0xFFFFFF
                      and c <= int(prefs().get("iso_ceiling_" + mode, capture.ISO_CEILING[mode])))
        cur_ap, cur_iso = props[PROPS["aperture"]].value, props[PROPS["iso"]].value
        shot = (autof.decision or {}).get("shot") or "interview"
        floor = int((capture.PROFILES[shot][mode].get("open_to") or 0) * 100)
        ch = []
        if stops > 0:                                    # brighter: iris first, ISO only as the last step
            cands = [a for a in aps if max(floor, aps[0] if aps else 0) <= a <= cur_ap]
            if cands and cur_ap > min(cands):
                want = cur_ap / (2 ** (stops / 2))
                best = min(cands, key=lambda a: abs(a - want))
                if best != cur_ap:
                    ch.append({"name": "aperture", "value": best})
                    stops -= 2 * math.log2(cur_ap / best)
            if stops >= 0.3 and isos and 0 < cur_iso < 0xFFFFFF:
                want = cur_iso * 2 ** stops
                best = min(isos, key=lambda v: abs(math.log2(v / want)))
                if best != cur_iso:
                    ch.append({"name": "iso", "value": best})
        else:                                            # darker: ISO back toward base first, then stop down
            if isos and 0 < cur_iso < 0xFFFFFF and cur_iso > isos[0]:
                want = max(isos[0], cur_iso * 2 ** stops)
                best = min(isos, key=lambda v: abs(math.log2(v / want)))
                ch.append({"name": "iso", "value": best})
                stops -= math.log2(best / cur_iso)
            if stops <= -0.3:
                cands = [a for a in aps if cur_ap < a <= 800]
                if cands:
                    want = cur_ap * 2 ** (-stops / 2)
                    ch.append({"name": "aperture", "value": min(cands, key=lambda a: abs(a - want))})
        return ch

    autof = autofix.AutoFix(studio, changelog, _analyze, _values, follower, dirx,
                           setup=lambda mode: builtin_setup(mode)[0],
                           has_key=lambda: assistant.has_key() and prefs().get("claude_on_connect", True),
                           goal_extra=post_goal, scene_fn=_scene, decide_fn=_decide,
                           apply_fn=_apply_proposal, prefs=prefs,
                           labels_fn=lambda: {s["name"]: s["label"] for s in settings_view(studio.props(max_age=3))})
    autof.plan_text = _light_plan_text
    if start_auto:
        autof.start()

    async def autofix_view(request: Request):
        """GET: AUTO status + recent automatic changes. POST {enabled?, auto_setup?, resume?, max_iso?}."""
        if request.method == "POST":
            body = await request.json()
            if "enabled" in body:
                autof.enabled = bool(body["enabled"])
                if autof.enabled:
                    autof.resume()
            if "auto_setup" in body:
                autof.auto_setup = bool(body["auto_setup"])
            if body.get("resume"):
                autof.resume()
            for m in ("photo", "video"):
                v = (body.get("max_iso") or {}).get(m) if isinstance(body.get("max_iso"), dict) else None
                if v:
                    autof.max_iso[m] = int(v)
            if body.get("apply") is not None:
                await anyio.to_thread.run_sync(lambda: autof.apply_proposal(int(body["apply"])))
            if body.get("dismiss"):
                autof.dismiss()
            if body.get("analyse"):
                autof.analyse_again(body.get("shot") if body.get("shot") in capture.PROFILES else None)
            p2 = {k: bool(body[k]) for k in ("allow_while_recording", "auto_apply") if k in body}
            if p2:
                save_prefs(**p2)
        p = prefs()
        return JSONResponse({**autof.status(), "allow_while_recording": bool(p.get("allow_while_recording")),
                             "auto_apply": p.get("auto_apply", False)})

    # -- SIMPLE MODE: live coach + "Make it look good" ------------------------
    coach_cache = {"t": 0.0, "seq": -1, "data": None}

    def coach_view(_: Request):
        """Checks on the newest live-view frame (no extra camera traffic; settings cached)."""
        frame, seq = studio.frame, studio.frame_seq
        if frame is None:
            return JSONResponse({"checks": [], "line": "Waiting for the camera…", "overlays": {}})
        if coach_cache["data"] is not None and (seq == coach_cache["seq"] or time.time() - coach_cache["t"] < 2.5):   # CPU-heavy: at most every 2.5 s
            return JSONResponse(coach_cache["data"])
        try:
            props = studio._props if studio._props is not None else studio.props(max_age=6)
            values = {s["name"]: s["value"] for s in settings_view(props)}
            data = coach.analyze(frame, values, status_view(props))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"checks": [], "line": f"Coach unavailable: {exc}", "overlays": {}})
        coach_cache.update(t=time.time(), seq=seq, data=data)
        return JSONResponse(data)

    async def coach_fix(request: Request):
        body = await request.json()
        kind = body.get("kind")

        def work():
            follower.pause(10)
            if kind == "exposure":
                props = studio.props(max_age=0)
                plan = coach.plan_exposure_fix(props, float(body.get("stops", 0)), int(body.get("max_iso") or 6400))
                if not plan:
                    return {"applied": [], "failed": [], "note": "Can't go further with ISO/aperture: change the light instead."}
                r = changelog.apply([{"name": n, "value": v} for n, v in plan], "coach",
                                    f"Coach: exposure {float(body.get('stops', 0)):+.1f} stops")
                return {**r, "note": ""}
            if kind == "focus":
                refocus_target()
                return {"applied": ["focus"], "failed": [], "note": ""}
            raise ValueError("unknown fix")
        try:
            coach_cache["data"] = None
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def simple_auto(request: Request):
        """One tap: Claude's Director if a key is set, else the built-in autopilot.
        Either way every change is logged (undo) and the 'before' frame is kept for the slider."""
        body = await request.json()
        mode = "video" if body.get("mode") == "video" else "photo"
        goal = str(body.get("goal", ""))[:600]
        preset = body.get("preset")
        look = body.get("look") if body.get("look") in presets.LOOKS else None
        if assistant.has_key() and not body.get("local"):
            try:
                dirx.start((goal or "Set the camera up for a technically perfect, neutral capture of this scene.") + post_goal(), mode,
                           feature="clean capture")
                return JSONResponse({"via": "director", "run": dirx.status["run"]})
            except RuntimeError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        def work():
            e, rep, key = builtin_setup(mode, preset, look)
            return {"via": "auto", "entry": e["id"], "preset": key, "warning": rep.get("warning", ""),
                    "tips": rep.get("tips", []), "verdict": rep.get("verdict")}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- ONE-BUTTON VIDEO: teleprompter script, clip pick-up, hand-off ---------
    vjobs = video.VideoJobs()

    def video_status(_: Request):
        return JSONResponse({**vjobs.status, "hdmi_pref": bool(prefs().get("hdmi"))})

    async def video_cancel(_: Request):
        vjobs.cancel()
        return JSONResponse({"ok": True})

    async def video_script(request: Request):
        """Teleprompter text written by Claude, only when the owner asks (one call)."""
        body = await request.json()
        topic = str(body.get("topic", ""))[:600]
        secs = int(min(max(int(body.get("seconds") or 60), 15), 600))
        lang = str(body.get("language", "")).strip()[:20]

        def work():
            key = assistant._key()
            if not key:
                raise assistant.AskError("NO_KEY")
            text = (f"Write a spoken script for a talking-head video of about {secs} seconds "
                    f"(about {int(secs * 2.4)} words) on: {topic or 'introducing myself and my work'}. "
                    + (f"Language: {lang}. " if lang else "Use the language of the topic. ")
                    + "Natural spoken style, short sentences, a hook in the first line, no stage directions, "
                      "no headings, no emojis. Return only the script text.")
            with assistant.feature("teleprompter"):
                data = assistant.call({"model": assistant.MODEL, "max_tokens": 4000, "output_config": {"effort": "low"},
                                   "fallbacks": "default", "messages": [{"role": "user", "content": text}]}, key)
            if data.get("stop_reason") == "refusal":
                raise assistant.AskError("Claude declined this topic. Try wording it differently.")
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        try:
            return JSONResponse({"script": await anyio.to_thread.run_sync(work)})
        except assistant.AskError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- TAKES: log, automatic card import, shot list, take review -------------
    sjobs = audiocheck.SoundJobs()          # a sound report per imported clip (on the copy; never Claude)

    def _on_imported(dst, take):
        if dst.suffix.lower() in importer.VIDEO_EXT:
            try:
                sjobs.add(dst, Path(dst).parent.parent)       # <shoot>\VIDEO\clip -> <shoot>
            except Exception:  # noqa: BLE001
                pass
        if dst.suffix.lower() in importer.VIDEO_EXT and vjobs.status["state"] == "waiting_clip":
            vjobs._imported(dst, {"log": True})
    imp = importer.Importer(on_imported=_on_imported)
    vjobs.external_import = True
    if start_auto:
        imp.start()

    def take_measure(frame: bytes) -> tuple[dict, dict]:
        props = studio.props(max_age=3)
        values = {s["name"]: s["value"] for s in director_settings_view(props)}
        analysis = coach.analyze(frame, {k: values.get(k) for k in values}, status_view(props))
        ex = next((c for c in analysis["checks"] if c["key"] == "exposure"), {})
        box = analysis.get("overlays", {}).get("subject")
        m = {"ire": ex.get("ire"), "cast": shotplan.colour_cast(frame, box), "values": values,
             "labels": {s["name"]: s["label"] for s in settings_view(props)}}
        return analysis, m

    def record_take(kind: str, start: float, end: float, frame: bytes | None, files: list[str]):
        """Log a take, store a reference frame, review it locally; tick the shot list."""
        if frame is None:
            return None
        try:
            analysis, m = take_measure(frame)
        except Exception:  # noqa: BLE001
            analysis, m = {"checks": []}, {}
        plan = shotplan.load()
        cur = plan.get("current")
        shot = plan["shots"][cur]["name"] if cur is not None and cur < len(plan.get("shots", [])) else ""
        prev = [t for t in importer.takes() if t.get("measure")]
        t = importer.log_take(kind, start, end, shot, m.get("labels"), m, files)
        sh = Path(handoff.ensure_shoot()["path"])
        (sh / "NOTES" / "takes").mkdir(parents=True, exist_ok=True)
        (sh / "NOTES" / "takes" / f"take{t['id']:03d}.jpg").write_bytes(frame)
        review = shotplan.local_review(analysis, m, prev)
        importer.update_take(t["id"], review=review)
        shotplan.tick(t)
        if prefs().get("auto_review") and assistant.has_key():
            threading.Thread(target=lambda: _claude_review(t["id"]), daemon=True).start()
        return t

    def _claude_review(tid: int) -> dict:
        t = next(x for x in importer.takes() if x["id"] == tid)
        sh = Path(handoff.ensure_shoot()["path"])
        frame = (sh / "NOTES" / "takes" / f"take{tid:03d}.jpg").read_bytes()
        meas = f"face {t['measure'].get('ire')} IRE, colour cast {t['measure'].get('cast')}"
        with assistant.feature("take review"):
            r = shotplan.claude_review(frame, director_settings_view(studio.props()), meas, t.get("shot", ""))
        importer.update_take(tid, claude=r)
        return r

    def takes_view(_: Request):
        sh = handoff.current()
        items = importer.takes(sh) if sh else []
        sound = audiocheck.load(Path(sh["path"])) if sh else {}
        out = []
        for t in reversed(items[-40:]):
            names = [Path(f).name for f in (t.get("files") or [])] + ([t["card_file"]] if t.get("card_file") else [])
            rep = next((sound[n] for n in names if n in sound), None)
            out.append({**t, "sound": rep} if rep else t)
        return JSONResponse({"takes": out, "importer": imp.status, "sound": sjobs.status,
                             "pending": [t["id"] for t in items if not t.get("imported")]})

    async def claude_model(request: Request):
        """All settings › Studio › Claude › Model: his choice (null = Studio's choice, Claude Opus 5.5)."""
        body = await request.json()
        m = body.get("model")
        if m is not None and m not in assistant.MODELS:
            return JSONResponse({"error": "That model isn't offered here."}, status_code=400)
        now = assistant.choose_model(m)
        autof._event(f"You chose {assistant.MODELS[now]} for Claude", kind="you")
        return JSONResponse(assistant.usage.today())

    def sound_cancel(_: Request):
        sjobs.cancel()
        return JSONResponse({"ok": True, **sjobs.status})

    def take_sound(request: Request):
        """His press in a take's Sound block: make (or make again) the sound report of that take's clip."""
        tid = int(request.path_params["tid"])
        sh = handoff.current()
        t = next((x for x in (importer.takes(sh) if sh else []) if x["id"] == tid), None)
        clips = [Path(f) for f in ((t or {}).get("files") or []) if Path(f).suffix.lower() in importer.VIDEO_EXT]
        clips = [c for c in clips if c.exists()]
        if not clips:
            return JSONResponse({"error": "This take's clip isn't in the shoot folder yet. It is checked once it is imported."},
                                status_code=400)
        sjobs.add(clips[0], Path(sh["path"]), again=True)
        return JSONResponse({"ok": True, "message": f"Listening to {clips[0].name}…"})

    async def take_match(request: Request):
        tid = int(request.path_params["tid"])
        t = next((x for x in importer.takes() if x["id"] == tid), None)
        cont = ((t or {}).get("review") or {}).get("continuity")
        if not t or not cont:
            return JSONResponse({"error": "nothing to match"}, status_code=400)
        prev = next(x for x in importer.takes() if x["id"] == cont["previous"])

        def work():
            follower.pause(10)
            frame = studio.frame
            _a, now = take_measure(frame) if frame else ({}, t["measure"])
            r = changelog.apply(shotplan.match_changes(prev, now.get("values", {})), "review", f"Match take {prev['id']}")
            if now.get("ire") is not None and prev["measure"].get("ire") is not None:
                per_stop = 8.9 if camprofile.is_log(camprofile.of(studio), now["values"].get("picture_profile")) else 7.0
                stops = (prev["measure"]["ire"] - now["ire"]) / per_stop
                if abs(stops) >= 0.3:
                    plan = coach.plan_exposure_fix(studio.props(max_age=0), stops)
                    r2 = changelog.apply([{"name": n, "value": v} for n, v in plan], "review", f"Match take {prev['id']} exposure")
                    r["applied"] += r2["applied"]
                    r["failed"] += r2["failed"]
            return r
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def take_claude(request: Request):
        tid = int(request.path_params["tid"])
        try:
            return JSONResponse(await anyio.to_thread.run_sync(lambda: _claude_review(tid)))
        except assistant.AskError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (StopIteration, OSError):
            return JSONResponse({"error": "unknown take"}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def plan_view(request: Request):
        if request.method == "POST":
            body = await request.json()
            try:
                p = await anyio.to_thread.run_sync(lambda: _with("shot list", lambda: shotplan.make_plan(
                    str(body.get("description", ""))[:800], "video" if body.get("mode") == "video" else "photo")))
            except assistant.AskError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse(p)
        return JSONResponse(shotplan.load() if handoff.current() else {"title": "", "shots": [], "current": None})

    async def plan_current(request: Request):
        i = int((await request.json()).get("index", 0))
        p = shotplan.load()
        if 0 <= i < len(p["shots"]):
            p["current"] = i
            shotplan.save(p)
        return JSONResponse(p)

    async def plan_setup(request: Request):
        """Set the camera up for one shot: one Claude call (validated changes), or a built-in preset."""
        i = int((await request.json()).get("index", 0))
        p = shotplan.load()
        if not 0 <= i < len(p["shots"]):
            return JSONResponse({"error": "unknown shot"}, status_code=400)
        s = p["shots"][i]
        p["current"] = i
        shotplan.save(p)

        def work():
            if assistant.has_key():
                frame = live_frame()
                with assistant.feature("shot set-up"):
                    r = assistant.ask(frame, settings_view(studio.props(max_age=0)), shot_request_text(s))
                res = changelog.apply([{"name": c["name"], "value": c["value"]} for c in r["changes"]], "plan",
                                      f"Shot “{s['name']}”: " + (r.get("explanation") or "")[:200])
                if prefs()["post"]:
                    apply_post(s["kind"])
                return {"via": "claude", **res, "explanation": r.get("explanation", ""), "tips": r.get("tips", "")}
            e, rep, key = builtin_setup(s["kind"], shotplan.builtin_preset(s))
            return {"via": "auto", "applied": [e["text"]], "failed": [], "explanation": f"Built-in preset {key}.",
                    "tips": " ".join(rep.get("tips", [])[:2])}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except assistant.AskError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    shot_request_text = shotplan.shot_request

    # -- SMART PHOTO: shoots by itself only while the owner has armed it -----------
    smart = smartphoto.SmartPhoto(studio, _analyze, lambda: take_photo())

    async def smart_view(request: Request):
        if request.method == "POST":
            body = await request.json()
            if body.get("armed"):
                if autof.mode(_values()) != "photo":
                    return JSONResponse({"error": "Smart photo works in photo mode. Switch to PHOTO first."}, status_code=400)
                smart.arm(int(body.get("limit") or 12))
            else:
                smart.disarm()
        return JSONResponse(smart.status)

    # -- HAND-OFF: shoot folders, Resolve, Lightroom / Photoshop ---------------
    def shoot_view(_: Request):
        sh = handoff.current()
        return JSONResponse({"shoot": sh, "summary": handoff.summary(Path(sh["path"])) if sh else None,
                             "root": str(handoff.SHOOTS), "resolve_installed": handoff.RESOLVE.exists(),
                             "photoshop_installed": handoff.PHOTOSHOP.exists()})

    async def shoot_new(request: Request):
        body = await request.json()
        return JSONResponse(handoff.new_shoot(str(body.get("name", "Shoot"))))

    def shoot_open(_: Request):
        sh = handoff.ensure_shoot()
        handoff.show_folder(Path(sh["path"]))
        return JSONResponse({"ok": True, "path": sh["path"]})

    async def shoot_export(request: Request):
        """Notes + camera settings + lighting plan/diagram + LUTs + Lightroom preset into the shoot folder."""
        body = await request.json()

        def work():
            sh = Path(handoff.ensure_shoot()["path"])
            try:
                settings = director_settings_view(studio.props(max_age=0))
            except Exception:  # noqa: BLE001 - camera offline: notes without settings
                settings = []
            handoff.write_notes(sh, settings, str(body.get("notes", ""))[:4000], body.get("plan"), str(body.get("svg", ""))[:2_000_000])
            key = body.get("look")
            look = presets.LOOKS.get(key) if key else None
            files = handoff.export_luts(sh, key, look)
            if look:
                files.append(handoff.export_xmp(sh, look))
            return {"folder": str(sh), "files": files}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    def shoot_resolve(_: Request):
        sh = Path(handoff.ensure_shoot()["path"])
        return JSONResponse(handoff.send_to_resolve(sh))

    def _raw_for(stem: str):
        """The RAW of the same shot (ARW, CR3, NEF...): same name, or same time stamp (files are saved as
        <time>_1.arw, <time>_2.jpg)."""
        from .drivers.base import RAW_EXTS
        for e in sorted(RAW_EXTS):
            for q in (CAPTURE_DIR / f"{stem}{e}", CAPTURE_DIR / f"{stem}{e.upper()}"):
                if q.exists():
                    return q
        base = stem.rsplit("_", 1)[0] if "_" in stem else stem
        return next(iter(sorted(p for p in CAPTURE_DIR.glob(base + "_*") if p.suffix.lower() in RAW_EXTS)), None)

    async def photoshop(request: Request):
        body = await request.json()
        src = (CAPTURE_DIR / str(body.get("file", ""))).resolve()
        if src.parent != CAPTURE_DIR.resolve() or not src.is_file():
            return JSONResponse({"error": "unknown photo"}, status_code=400)
        stem = src.stem.split("__")[0]
        raw = _raw_for(stem)
        try:
            handoff.open_in_photoshop(raw or src)
            return JSONResponse({"ok": True, "opened": (raw or src).name})
        except FileNotFoundError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def _raw_preview(arw: Path) -> bytes | None:
        """The largest JPEG embedded in the ARW (the camera's own preview). Not a RAW development."""
        data = arw.read_bytes()
        best, i = None, 0
        while True:
            s = data.find(b"\xff\xd8\xff", i)
            if s < 0:
                break
            e = data.find(b"\xff\xd9", s + 3)
            while e > 0:
                cand = data[s:e + 2]
                try:
                    from PIL import Image as _I
                    _I.open(io.BytesIO(cand)).verify()
                    if best is None or len(cand) > len(best):
                        best = cand
                    break
                except Exception:  # noqa: BLE001
                    e = data.find(b"\xff\xd9", e + 2)
            i = s + 3
        return best

    def _clip_rgb(jpeg: bytes) -> list[float]:
        from PIL import Image as _I
        im = _I.open(io.BytesIO(jpeg)).convert("RGB")
        im.thumbnail((800, 800))
        px = im.tobytes()
        n = max(1, len(px) // 3)
        return [round(100 * sum(1 for v in px[c::3] if v >= 250) / n, 2) for c in range(3)]

    def raw_review(request: Request):
        """For the photo review: the RAW's embedded preview + clipping. A real RAW development needs a decoder
        (rawpy/LibRaw) that is not installed; until then this is clearly labelled as the camera's preview."""
        src = (CAPTURE_DIR / request.path_params["name"]).resolve()
        if src.parent != CAPTURE_DIR.resolve() or not src.is_file():
            return JSONResponse({"error": "unknown photo"}, status_code=400)
        stem = src.stem.split("__")[0]
        raw = _raw_for(stem)
        out = {"raw": raw.name if raw else None, "developed": False,
               "clip_jpeg": _clip_rgb(src.read_bytes()) if src.suffix.lower() == ".jpg" else None}
        if raw:
            pv = _raw_preview(raw)
            if pv:
                d = CAPTURE_DIR / "_rawpreview"
                d.mkdir(exist_ok=True)
                (d / f"{stem}.jpg").write_bytes(pv)
                from PIL import Image as _I
                w, h = _I.open(io.BytesIO(pv)).size
                out.update(preview=f"_rawpreview/{stem}.jpg", size=[w, h], clip_preview=_clip_rgb(pv),
                           label="Camera's embedded preview, not a RAW development")
        return JSONResponse(out)

    # -- installable app (iPad "Add to Home Screen") + client config ------------------------------------
    _icons: dict[int, bytes] = {}

    def _icon(size: int) -> bytes:
        if size not in _icons:
            from PIL import Image as _I, ImageDraw as _D
            im = _I.new("RGB", (size, size), (11, 12, 14))
            d = _D.Draw(im)
            r = size * 0.34
            c = size / 2
            d.ellipse((c - r, c - r, c + r, c + r), outline=(232, 176, 74), width=max(2, size // 22))
            d.ellipse((c - r * 0.45, c - r * 0.45, c + r * 0.45, c + r * 0.45), fill=(232, 176, 74))
            b = io.BytesIO()
            im.save(b, "PNG")
            _icons[size] = b.getvalue()
        return _icons[size]

    def icon_view(request: Request):
        size = int(request.path_params.get("size", 180))
        return Response(_icon(size if size in (180, 192, 512) else 180), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})

    def manifest_view(_: Request):
        return JSONResponse({"name": "camctl Studio", "short_name": "Studio", "start_url": "/", "scope": "/",
                             "display": "fullscreen", "orientation": "any", "background_color": "#0b0c0e",
                             "theme_color": "#0b0c0e",
                             "icons": [{"src": f"/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png"} for s in (180, 192, 512)]},
                            media_type="application/manifest+json")

    def client_config(_: Request):
        from .drivers.sony import S1_SETTLE_S
        return JSONResponse({"shutter_hold_ms": int(S1_SETTLE_S * 1000), "lan": dict(LAN_STATE)})

    async def light_view(_: Request):
        """What the light does right now (local measurement on the live picture, nothing is changed)."""
        def work():
            frame, _ = studio.latest_frame(-1, timeout=2.0)
            if frame is None:
                raise ValueError("No live picture yet.")
            s = _scene(frame)
            return {k: s.get(k) for k in ("subject", "key", "background", "backlit", "light_colour", "flicker", "overall_ire", "log")}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- diagnostics: read-only protocol info, and fetching an image that is already waiting -------------
    def ext_info(_: Request):
        """Sony GetExtDeviceInfo (read-only): protocol version, supported properties and controls."""
        import struct as _s

        def work(c):
            from .drivers.sony import OC_SDIO_GET_EXT_DEVICE_INFO
            for ver in (0x012C, 0x00C8):
                r = c.client.transaction(OC_SDIO_GET_EXT_DEVICE_INFO, [ver])
                if r.ok:
                    return ver, r.data
            return None, b""
        try:
            ver, d = studio.run(work)
        except Exception as exc:  # noqa: BLE001 - no camera, or a camera without this Sony request
            return JSONResponse({"error": f"The camera's device info could not be read ({exc})."}, status_code=409)
        out = {"asked_version": hex(ver) if ver else None, "bytes": len(d)}
        try:
            pos = 0
            out["camera_version"] = _s.unpack_from("<H", d, pos)[0]; pos += 2
            lists = []
            while pos + 4 <= len(d):
                n = _s.unpack_from("<I", d, pos)[0]; pos += 4
                if n > 2000 or pos + 2 * n > len(d):
                    break
                lists.append([f"0x{v:04X}" for v in _s.unpack_from(f"<{n}H", d, pos)]); pos += 2 * n
            out["lists"] = lists
        except Exception as exc:  # noqa: BLE001
            out["parse_error"] = str(exc)
        return JSONResponse(out)

    diag_shots = {"n": int(os.environ.get("CAMCTL_DIAG_USED", "0") or 0)}

    async def shutter_variant(request: Request):
        """ONE diagnostic shutter attempt (approved budget: 5). Variants change one thing at a time."""
        from .drivers.sony import (CTRL_FULL_PRESS, CTRL_HALF_PRESS, PRESS, RELEASE, _clog)
        body = await request.json()
        v = str(body.get("variant", ""))
        if diag_shots["n"] >= 5:
            return JSONResponse({"error": "budget of 5 used"}, status_code=409)
        diag_shots["n"] += 1
        _clog(f"=== DIAG ATTEMPT {diag_shots['n']}/5 variant={v} reason={body.get('reason', '')}")

        def work(c):
            def watch():
                pr = c.refresh()
                return {k: getattr(pr.get(code), "value", None) for k, code in
                        (("pending", SP_PENDING_IMAGES), ("shots", SP_SHOTS_LEFT), ("focus_ind", SP_FOCUS_INDICATION), ("af_lock", SP_AF_LOCK), ("status", SP_STATUS))}
            before = watch()
            _clog("before", **before)
            if v == "afs_wait_focus":
                c.set("focus_mode", 2)                      # AF-S, area stays Spot
                time.sleep(0.5)
                c._control(CTRL_HALF_PRESS, PRESS)
                t0 = time.time()
                while time.time() - t0 < 3:
                    w = watch()
                    if w["focus_ind"] != before["focus_ind"]:
                        _clog("focus indication changed", **w)
                        break
                    time.sleep(0.15)
                c._control(CTRL_FULL_PRESS, PRESS); time.sleep(0.5)
                c._control(CTRL_FULL_PRESS, RELEASE); c._control(CTRL_HALF_PRESS, RELEASE)
            elif v == "mf_s1_hold3":
                c.set("focus_mode", 1)                      # MF (Studio's lock), only the S1 hold changes
                time.sleep(0.5)
                c._control(CTRL_HALF_PRESS, PRESS); time.sleep(3.2)
                c._control(CTRL_FULL_PRESS, PRESS); time.sleep(0.5)
                c._control(CTRL_FULL_PRESS, RELEASE); c._control(CTRL_HALF_PRESS, RELEASE)
            elif v == "still_d2c7":
                c._control(CTRL_HALF_PRESS, PRESS); time.sleep(0.4)
                c._control(SC_STILL_D2C7, PRESS); time.sleep(0.5)
                c._control(SC_STILL_D2C7, RELEASE); c._control(CTRL_HALF_PRESS, RELEASE)
            elif v == "mf_hold_long":
                c._control(CTRL_HALF_PRESS, PRESS); time.sleep(0.4)
                c._control(CTRL_FULL_PRESS, PRESS); time.sleep(1.2)
                c._control(CTRL_FULL_PRESS, RELEASE); c._control(CTRL_HALF_PRESS, RELEASE)
            elif v == "s2_only":
                c._control(CTRL_FULL_PRESS, PRESS); time.sleep(0.5)
                c._control(CTRL_FULL_PRESS, RELEASE)
            else:
                raise ValueError("unknown variant")
            fired = False
            after = before
            t0 = time.time()
            while time.time() - t0 < 12:
                after = watch()
                if (after["pending"] or 0) & 0x8000 or (after["shots"] is not None and before["shots"] is not None
                                                        and after["shots"] != before["shots"]):
                    fired = True
                    break
                time.sleep(0.4)
            _clog("result", fired=fired, **after)
            files = c.fetch_pending(timeout=15) if fired else []
            return fired, before, after, files
        try:
            fired, before, after, files = studio.run(work)
        except Exception as exc:  # noqa: BLE001
            _clog("error", msg=str(exc))
            return err(exc)
        saved = save_files(files)
        copies = []
        for name in saved:
            try:
                copies.append(str(handoff.add_file(CAPTURE_DIR / name)))
            except OSError:
                pass
        return JSONResponse({"attempt": diag_shots["n"], "variant": v, "fired": fired, "before": before, "after": after,
                             "saved": saved, "sizes": [(CAPTURE_DIR / n).stat().st_size for n in saved], "shoot": copies})

    def fetch_waiting(_: Request):
        """Transfer images the camera already has waiting for the PC (e.g. a shot taken on the body while
        'save to PC' is set). This is a transfer, never a shot."""
        try:
            files = studio.run(lambda c: c.fetch_pending(timeout=12))
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        saved = save_files(files)
        copies = []
        for name in saved:
            try:
                copies.append(str(handoff.add_file(CAPTURE_DIR / name)))
            except OSError:
                pass
        return JSONResponse({"saved": saved, "sizes": [(CAPTURE_DIR / n).stat().st_size for n in saved], "shoot": copies})

    # -- CAMERA CHECK: the whole non-shooting verification, one button ------------------------------
    camcheck = {"running": False, "lines": [], "done": None}

    def _dump_props(tag: str) -> list[dict]:
        props = studio.run(lambda c: c.refresh())
        by_code = {code_of(n): n for n in CONTROLS}
        out = []
        for code, pr in sorted(props.items()):
            name = by_code.get(code)
            lab = (lambda v, name=name: label(name, v)) if name else str
            out.append({"code": f"0x{code:04X}", "name": KNOWN_PROPS.get(code, f"0x{code:04X}"), "value": pr.value,
                        "label": lab(pr.value), "writable": pr.writable, "enabled": pr.enabled,
                        "choices": [{"value": c, "label": lab(c)} for c in pr.choices],
                        "range": list(pr.range) if pr.range else None})
        if "simulated" not in str(getattr(studio.cam, "name", "")):     # only real-camera lists feed the simulator
            d = Path(__file__).resolve().parents[1] / "tests" / "data"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"zve10_{tag}_props.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        return out

    def _camcheck_run() -> None:
        L = camcheck["lines"]
        ok_all = True

        def line(status: str, text: str) -> None:
            nonlocal ok_all
            ok_all = ok_all and status != "FAIL"
            L.append(f"- **{status}** {text}")

        try:
            h = studio.health()
            line("OK" if h.get("state") == "ok" and studio.cam else "FAIL",
                 f"Connection: {'USB' if studio.host == 'usb' else 'Wi-Fi ' + studio.host}, state {h.get('state')}")
            v = _values()
            start_mode = autof.mode(v)
            f0, _ = studio.latest_frame(-1, timeout=4)
            line("OK" if f0 else "FAIL", f"Live view: {'frames arriving' if f0 else 'no frames'} ({studio.fps:.1f} fps)")
            for m in ("photo", "video"):
                if m == "video":
                    studio.run(lambda c: c.set(PROPS["exposure_mode"], capture.MOVIE_M))
                else:
                    studio.run(lambda c: c.set(PROPS["exposure_mode"], capture.PHOTO_M))
                time.sleep(1.0)
                verdict_m = mode_setup(m, who="Camera check")
                dump = _dump_props(m + ("_pp8" if m == "video" else "_std"))
                vals = {s["name"]: s["label"] for s in settings_view(studio.props(max_age=0))}
                bad = [i["text"] for i in verdict_m["items"] if i["status"] == "bad"]
                line("OK" if not bad else "WARN", f"{m.title()} setup: " + ", ".join(
                    f"{k}={vals.get(k)}" for k in ("exposure_mode", "aperture", "shutter_speed", "iso", "picture_profile",
                                                   "focus_mode", "focus_area", "white_balance")) + (f" · problems: {'; '.join(bad)}" if bad else ""))
                line("OK" if vals.get("focus_mode") == "MF" or "MF" in str(vals.get("focus_mode")) else "FAIL",
                     f"{m.title()} focus locked after setup: {vals.get('focus_mode')}")
                pp = vals.get("picture_profile", "")
                mine = formats.chosen(m, "picture_profile")
                if mine is not None:                              # his own format choice: that profile, whatever it is
                    ppv = (studio.props(max_age=0).get(presets.PROP_PICTURE_PROFILE) or None)
                    line("OK" if ppv is not None and ppv.value == mine else "FAIL", f"{m.title()} picture profile: {pp}")
                else:
                    want_log = m == "video" or bool(prefs().get("photo_log"))
                    line("OK" if ("S-Log3" in str(pp)) == want_log else "FAIL", f"{m.title()} picture profile: {pp}")
                iso_c = next((d for d in dump if d["code"] == "SP_ISO"), {})
                isos = [c["value"] for c in iso_c.get("choices", []) if 0 < c["value"] < 0xFFFFFF]
                sh = next((d for d in dump if d["code"] == "SP_SHUTTER"), {})
                line("OK", f"{m.title()} allowed values recorded: ISO {min(isos) if isos else '?'}–{max(isos) if isos else '?'}, "
                     f"{len(sh.get('choices', []))} shutter speeds (saved for the simulator)")
            if start_mode != "video":                             # back to where he was
                studio.run(lambda c: c.set(PROPS["exposure_mode"], capture.PHOTO_M))
                time.sleep(1.0)
                mode_setup("photo", who="Camera check")
            line("OK", f"Back in {start_mode} mode, Locked. No photo, no recording.")
        except Exception as exc:  # noqa: BLE001
            line("FAIL", f"Stopped: {exc}")
        camcheck["done"] = time.time()
        camcheck["ok"] = ok_all
        try:
            with CAMCHECK_AUDIT.open("a", encoding="utf-8") as fh:
                sim = " (SIMULATED camera)" if "simulated" in str(getattr(studio.cam, "name", "")) else ""
                fh.write(f"\n\n## Camera check {time.strftime('%Y-%m-%d %H:%M')}{sim} ({'all OK' if ok_all else 'problems found'})\n\n"
                         + "\n".join(L) + "\n")
        except OSError:
            pass
        camcheck["running"] = False

    async def camcheck_view(request: Request):
        """POST: run the camera check (no shooting). GET: progress / result."""
        if request.method == "POST" and not camcheck["running"]:
            if studio.recording:
                return JSONResponse({"error": "Not while recording."}, status_code=409)
            camcheck.update(running=True, lines=[], done=None)
            threading.Thread(target=_camcheck_run, daemon=True, name="camcheck").start()
        return JSONResponse({k: camcheck.get(k) for k in ("running", "lines", "done", "ok")})

    # -- SAVED SETUPS / SHOT LIST -------------------------------------------
    def setups_list(_: Request):
        return JSONResponse(setups.list_all())

    # -- ND FILTER ON THE LENS (his variable ND): a live reading for turning the ring, local, nothing written ----------
    nd_state: dict = {"on": bool(prefs().get("nd_on")), "aim": None, "aim_t": None, "off_level": None, "off_vals": None,
                      "ema": None, "target": None, "strength": None}
    ND_KEYS = ("aperture", "shutter_speed", "iso")

    def _expo_vals() -> dict:
        v = _values()
        return {k: v.get(k) for k in ND_KEYS}

    def nd_words(stops: float) -> str:
        if abs(stops) < 0.25:
            return "Right"
        return f"{abs(stops):.1f} stop{'s' if abs(stops) >= 1.5 else ''} too {'dark' if stops < 0 else 'bright'}"

    def nd_reading(level: float | None = None) -> dict:
        """How far the picture is from the brightness Studio's last setup aimed for (or a recalled setup's), smoothed.
        Estimated from the picture only: 'about'."""
        from . import resume
        out = {"on": nd_state["on"], "recall": nd_state["target"] is not None}
        if level is None:
            frame = studio.frame
            if frame is None:
                return {**out, "stops": None, "text": "No live picture yet."}
            level = resume.light_level(frame)
        b = autof.baseline or {}
        if nd_state["target"] is not None and (b.get("t") or 0) > nd_state.get("target_t", 0):
            nd_state.update(target=None, ema=None)             # a newer setup: its exposure is the aim again
        if nd_state["target"] is None and b.get("t") and b.get("t") != nd_state["aim_t"] and autof.phase == "locked" \
                and time.time() - b["t"] > 6:
            nd_state.update(aim=level, aim_t=b["t"], ema=None)   # the picture right after Studio's setup is "right"
        aim = nd_state["target"] if nd_state["target"] is not None else nd_state["aim"]
        if aim is None:
            return {**out, "stops": None, "text": "Press Analyse again once, so Studio knows the right brightness."}
        s = resume.stops_between(aim, level)
        e = s if nd_state["ema"] is None else 0.65 * nd_state["ema"] + 0.35 * s
        nd_state["ema"] = e
        out.update(stops=round(e, 2), text=nd_words(e))
        if nd_state["off_level"] is not None and nd_state["off_vals"] == _expo_vals():
            nd_state["strength"] = round(-resume.stops_between(nd_state["off_level"], level), 1)
        elif nd_state["strength"] is not None and nd_state["strength"] < 0.3:
            nd_state["strength"] = None                     # nothing measured yet, and the values changed since
        st = nd_state["strength"]
        out["strength"] = st
        out["strength_text"] = (f"The filter is taking about {st:g} stops." if st is not None and st >= 0.3 else
                                "The filter's strength is not known yet." if st is None else "The filter takes almost no light now.")
        try:
            mode = autof.mode(_values())
            ceiling = int(prefs().get("iso_ceiling_" + mode, capture.ISO_CEILING[mode]))
            iso = (studio.props(max_age=3).get(PROPS["iso"]) or None)
            if iso is not None and iso.value >= ceiling and e < -0.25:
                out["limit"] = "ISO is at your limit: turn the ND lighter or add light."
        except Exception:  # noqa: BLE001
            pass
        return out

    async def nd_view(request: Request):
        """GET: the live ND reading. POST {on}: the switch "ND filter on the lens" (his choice, nothing on the camera)."""
        if request.method == "POST":
            body = await request.json()
            on = bool(body.get("on"))
            save_prefs(nd_on=on)
            nd_state.update(on=on, ema=None)
            if on:
                from . import resume
                f = studio.frame
                nd_state.update(off_level=resume.light_level(f) if f else None, off_vals=_expo_vals(), strength=None)
            else:
                nd_state.update(target=None, off_level=None, off_vals=None, strength=None)
            autof._event("You put the ND filter on the lens" if on else "You took the ND filter off", kind="you")
        try:
            return JSONResponse(await anyio.to_thread.run_sync(nd_reading))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    FORMAT_KEYS = {"still_format", "movie_format", "movie_setting", "picture_profile", "save_to", "jpeg_quality", "image_size"}
    recall_state: dict = {"last": None}

    PROP_FOCUS_POS = SP_FOCUS_POSITION

    def focus_pos_now() -> int | None:
        """The lens focus position the camera reports: 0 nearest .. 100 infinity (relative, not metres)."""
        try:
            p = studio.run(lambda c: c.refresh().get(PROP_FOCUS_POS))
            return int(p.value) if p is not None and isinstance(p.value, int) else None
        except Exception:  # noqa: BLE001
            return None

    def focus_goto_pos(target: int) -> int | None:
        """Drive the focus back to a saved position with manual-focus steps, closing on the read-back. MF/DMF only."""
        fm = studio.props(max_age=0).get(PROPS["focus_mode"])
        if fm is None or fm.value not in (1, 32774):
            return None
        for step, tol in ((7, 12), (3, 4), (1, 1)):
            for _ in range(40):
                p = focus_pos_now()
                if p is None:
                    return None
                gap = target - p
                if abs(gap) <= tol:
                    break
                studio.run(lambda c, s=(step if gap > 0 else -step): c.focus_step(s))
                time.sleep(0.35)
        return focus_pos_now()

    def fresh_frame(n: int = 2, timeout: float = 3.0) -> bytes | None:
        """A live picture taken after now (n frames on), so it shows the values just set."""
        frame, seq = studio.latest_frame(-1, timeout=timeout)
        target = seq + n
        end = time.time() + timeout
        while seq < target and time.time() < end:
            frame, seq = studio.latest_frame(seq, timeout=max(0.1, end - time.time()))
        return frame

    def _setup_extra(body: dict, frame: bytes | None) -> dict:
        """What a saved setup needs besides the camera values (local only, no Claude)."""
        from . import resume
        prof = camprofile.of(studio)
        x: dict = {"shot": (autof.decision or {}).get("shot"), "shot_name": (autof.decision or {}).get("shot_name"),
                   "camera": prof.get("model"), "measure": body.get("measure") or {}}
        if body.get("object"):
            x["object"] = {"cm": body["object"].get("cm"), "box": body["object"].get("box")}
        try:
            pos, f = studio.run(_zoom_read)
            x["zoom"] = {"pos": pos, "mm": _mm(pos), "f": f} if pos is not None else None
        except Exception:  # noqa: BLE001
            x["zoom"] = None
        sh = handoff.current() or {}
        x["shoot"] = {"name": sh.get("name"), "path": sh.get("path")} if sh else None
        x["focus"] = {"text": focus_state.get("text") or "", "position": focus_pos_now(), "distance": None}
        x["nd"] = {"on": nd_state["on"], "variable": True, "strength": nd_state["strength"] if nd_state["on"] else None}
        try:                                                  # the format: his choices and what the camera records
            m = "video" if body.get("mode") == "video" else "photo"
            x["formats"] = {"mode": m, "chosen": formats.choices_made()[m],
                            "name": formats.summary(m, studio.props(max_age=0), prof, label)["name"]}
        except Exception:  # noqa: BLE001
            x["formats"] = None
        if frame:
            x["frame"] = resume.b64(resume.jpeg_q(frame, 1024, 90))       # the clean camera picture, no overlays
            try:
                x["outline"] = resume.b64(resume.outline_png(frame))
            except Exception:  # noqa: BLE001
                x["outline"] = None
            x["light"] = {"level": round(resume.light_level(frame), 2)}
            try:
                s = _scene(frame)
                x["light_measure"] = {k: s.get(k) for k in ("key", "background", "light_colour", "overall_ire")}
            except Exception:  # noqa: BLE001
                x["light_measure"] = None
            focal = (x.get("zoom") or {}).get("mm") and zoom_mm((x["zoom"] or {}).get("pos"))
            ob = x.get("object") or {}
            frames = [frame] + [f for f in (fresh_frame(1, 1.5), fresh_frame(1, 1.5)) if f]
            ests = [resume.estimate(f, focal, prof.get("sensor_w_mm"), measure._faces_cv, ob.get("cm"), ob.get("box"))
                    for f in frames]
            good = sorted((e for e in ests if e.get("m")), key=lambda e: e["m"])
            x["distance"] = good[len(good) // 2] if good else ests[0]   # the median of up to three pictures
        return x

    async def setups_save(request: Request):
        body = await request.json()

        def work():
            try:
                frame = fresh_frame() or live_frame()
            except Exception:  # noqa: BLE001 - a setup without a picture is fine
                frame = None
            return setups.save(str(body.get("name", "")), str(body.get("notes", "")),
                               "video" if body.get("mode") == "video" else "photo",
                               director_settings_view(studio.props(max_age=0)),
                               body.get("preset"), body.get("look"), body.get("plan") or getattr(studio, "last_plan", None),
                               _refs(body), frame, _setup_extra(body, frame))
        try:
            d = await anyio.to_thread.run_sync(work)
            return JSONResponse({"id": d["id"], "name": d["name"], "distance": d.get("distance")})
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    def setups_get(request: Request):
        try:
            return JSONResponse(setups.get(request.path_params["sid"]))
        except (KeyError, OSError):
            return JSONResponse({"error": "unknown setup"}, status_code=404)

    async def setups_update(request: Request):
        body = await request.json()
        try:
            d = setups.get(request.path_params["sid"])
        except (KeyError, OSError):
            return JSONResponse({"error": "unknown setup"}, status_code=404)
        fields = {k: body[k] for k in ("name", "notes", "measure") if k in body}
        if "object" in body:                                  # he entered an object size and marked it
            from . import resume
            ob = {"cm": body["object"].get("cm"), "box": body["object"].get("box")}
            fields["object"] = ob
            if d.get("frame"):
                prof = camprofile.of(studio)
                focal = zoom_mm((d.get("zoom") or {}).get("pos")) if (d.get("zoom") or {}).get("mm") else None
                fields["distance"] = resume.estimate(base64.b64decode(d["frame"]), focal, prof.get("sensor_w_mm"),
                                                     measure._faces_cv, ob.get("cm"), ob.get("box"))
        return JSONResponse(setups.update(request.path_params["sid"], **fields))

    async def setups_recall(request: Request):
        """His press only: the saved values exactly, the zoom back to its position, Locked. No Claude call.
        The format rules stay (photo RAW + JPEG, video S-Log3 4K 25p). A clearly different light is only said."""
        try:
            d = setups.get(request.path_params["sid"])
        except (KeyError, OSError):
            return JSONResponse({"error": "unknown setup"}, status_code=404)
        if dirx.status["running"]:
            return JSONResponse({"error": "Stop the director first."}, status_code=400)
        if studio.recording:
            return JSONResponse({"error": "Not while recording."}, status_code=409)
        from . import resume
        changes = setups.recall_changes(d, CONTROLS + list(WB_SHIFT))
        saved = {c["name"]: c["value"] for c in changes                       # only the values Studio knows by name
                 if c["name"] not in FORMAT_KEYS and not c["name"].startswith("0x")}

        def work():
            autof.studio_busy(60)
            follower.pause(30)
            before_ids = {e["id"] for e in changelog.entries}
            why = f"Recall setup “{d['name']}”"
            try:
                zb = studio.run(_zoom_read)[0]
            except Exception:  # noqa: BLE001
                zb = None
            mode_now = "video" if (changelog.snapshot().get("exposure_mode") or 0) >= 0x78050 else "photo"
            applied, failed = [], []
            if "exposure_mode" in saved:                      # the mode decides what the rest allow
                r = changelog.apply([{"name": "exposure_mode", "value": saved["exposure_mode"]}], "setup", why)
                applied += r["applied"]; failed += r["failed"]
            fsaved = d.get("formats") or {}
            if d.get("mode") in ("photo", "video") and isinstance(fsaved.get("chosen"), dict):
                # the setup's own format (his choices then; empty = Studio's choice), before the exposure values
                if d["mode"] != mode_now:
                    time.sleep(1.0)
                formats.set_mode_choices(d["mode"], fsaved["chosen"])
                try:
                    fixed = enforce_format(d["mode"])
                    if fixed:
                        applied.append("format")
                except Exception:  # noqa: BLE001
                    failed.append("format")
            elif d.get("mode") in ("photo", "video") and d["mode"] != mode_now:
                time.sleep(1.0)
                r = apply_post(d["mode"])                     # the file-format rules of that mode
                applied += r["applied"]; failed += r["failed"]
            z = d.get("zoom") or {}
            zoom_res = None
            if z.get("pos") is not None and zb is not None and z["pos"] != zb:
                zoom_res = zoom_goto_pos(int(z["pos"]))       # first: the lens limits the widest aperture
            fb = focus_pos_now()
            rest = [{"name": k, "value": v} for k, v in saved.items() if k != "exposure_mode"]
            r = changelog.apply(sorted(rest, key=lambda c: (CONTROLS + list(WB_SHIFT)).index(c["name"])
                                       if c["name"] in CONTROLS + list(WB_SHIFT) else 999), "setup", why)
            applied += r["applied"]; failed += r["failed"]
            fpos_saved = (d.get("focus") or {}).get("position")
            fpos = focus_goto_pos(int(fpos_saved)) if fpos_saved is not None and fb is not None and fb != fpos_saved else fb
            time.sleep(0.4)
            now = changelog.snapshot()
            back = {k: (now.get(k) == v) for k, v in saved.items()}
            try:
                zpos = studio.run(_zoom_read)[0]
            except Exception:  # noqa: BLE001
                zpos = None
            autof.decision = {**(autof.decision or {}), "shot": d.get("shot") or (autof.decision or {}).get("shot"),
                              "shot_name": d.get("shot_name") or (autof.decision or {}).get("shot_name")}
            autof.baseline = {"ire": None, "shot": d.get("shot"), "subject": None, "cast": None, "t": time.time(),
                              "mode": d.get("mode") or mode_now, "sharp": None, "size": None}
            autof.proposal = None
            autof.phase, autof.state = "locked", f"Locked: {d['name']}"
            try:
                autofix._save_fp(autof.values())
            except Exception:  # noqa: BLE001
                pass
            autof._event(f"You recalled the setup “{d['name']}”", kind="you")
            focus_state.update(text="not checked after the recall", point=None, tracking=False, t=time.time(), found=None)
            say, stops, nd_say = "", None, ""
            ndd = d.get("nd") or {}
            if ndd.get("on") and (d.get("light") or {}).get("level") is not None:
                # this shoot used the ND: guide the ring back to the saved brightness instead of "the light changed"
                nd_state.update(target=d["light"]["level"], target_t=time.time() + 1, ema=None)
                nd_say = ("This shoot used the ND at about " + f"{ndd['strength']:g} stops. " if ndd.get("strength") else
                          "This shoot used the ND. ") + "Turn the ring until it says Right."
            elif (d.get("light") or {}).get("level") is not None:
                time.sleep(0.8)                               # a fresh picture with the recalled values
                frame = fresh_frame(3)
                if frame:
                    stops = round(resume.stops_between(d["light"]["level"], resume.light_level(frame)), 2)
                    say = resume.light_sentence(stops)
            ids = [e["id"] for e in changelog.entries if e["id"] not in before_ids]
            recall_state["last"] = {"ids": ids, "zoom_before": zb, "focus_before": fb, "name": d["name"], "t": time.time()}
            return {"applied": applied, "failed": failed, "ids": ids, "read_back": back,
                    "all_back": all(back.values()) if back else True,
                    "zoom": {"saved": z.get("pos"), "now": zpos, "mm": _mm(zpos), "before": zb, "moved": zoom_res is not None},
                    "focus_pos": {"saved": fpos_saved, "now": fpos, "before": fb}, "nd_say": nd_say,
                    "light_stops": stops, "light_say": say, "focus": "Not checked", "state": autof.state,
                    "mode": d.get("mode"), "plan": d.get("plan"), "refs": d.get("refs") or [], "preset": d.get("preset"),
                    "look": d.get("look")}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    lineup_state: dict = {"ref": None, "name": ""}

    async def lineup_view(request: Request):
        """Line up with a saved setup's picture or the last take: one plain instruction at a time (local, no Claude).
        POST {setup: id} | {take: "last"} | {} (off). GET: {say, lined, scores}. Nothing on the camera changes."""
        from . import match, resume
        if request.method == "POST":
            body = await request.json()
            jpg, name = None, ""
            if body.get("setup"):
                try:
                    d = setups.get(str(body["setup"]))
                except (KeyError, OSError):
                    return JSONResponse({"error": "unknown setup"}, status_code=404)
                b = d.get("frame") or d.get("thumb")
                jpg, name = (base64.b64decode(b) if b else None), d["name"]
            elif body.get("take"):
                sh = handoff.current()
                takes = [t for t in importer.takes() if t.get("id")]
                if sh and takes:
                    tid = max(t["id"] for t in takes)
                    f = Path(sh["path"]) / "NOTES" / "takes" / f"take{tid:03d}.jpg"
                    if f.exists():
                        jpg, name = f.read_bytes(), f"take {tid}"
                if jpg is None:
                    return JSONResponse({"error": "No take in this shoot yet."}, status_code=409)
            if jpg is None:
                lineup_state.update(ref=None, name="")
                return JSONResponse({"ok": True, "on": False})
            lineup_state.update(ref=match.prepare(jpg), name=name)
            out = {"ok": True, "on": True, "name": name, "picture": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}
            try:
                out["outline"] = "data:image/png;base64," + base64.b64encode(resume.outline_png(jpg)).decode()
            except Exception:  # noqa: BLE001
                out["outline"] = None
            return JSONResponse(out)
        if lineup_state["ref"] is None:
            return JSONResponse({"on": False})

        def work():
            frame, _ = studio.latest_frame(-1, timeout=2.0)
            if frame is None:
                raise ValueError("No live picture yet.")
            pp = studio.props(max_age=5).get(presets.PROP_PICTURE_PROFILE)
            r = match.compare(lineup_state["ref"], frame, log=bool(pp and camprofile.is_log(camprofile.of(studio), pp.value)))
            say, lined = resume.line_up_say(r)
            return {"on": True, "name": lineup_state["name"], "say": say, "lined": lined, "framing": r["scores"]["framing"], "raw": r["raw"]}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def setups_undo(_: Request):
        """Undo the last recall: every value and the zoom as they were before it."""
        last = recall_state["last"]
        if not last:
            return JSONResponse({"error": "Nothing to undo."}, status_code=409)

        def work():
            autof.studio_busy(30)
            failed = changelog.undo(last["ids"])
            zres = None
            if last.get("zoom_before") is not None:
                try:
                    if studio.run(_zoom_read)[0] != last["zoom_before"]:
                        zres = zoom_goto_pos(int(last["zoom_before"]))
                except Exception:  # noqa: BLE001
                    pass
            if last.get("focus_before") is not None:
                focus_goto_pos(int(last["focus_before"]))
            recall_state["last"] = None
            autof.phase, autof.state = "locked", "Locked: as before the recall"
            return {"ok": not failed, "failed": failed, "zoom": zres}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def setups_delete(request: Request):
        try:
            setups.delete(request.path_params["sid"])
            return JSONResponse({"ok": True})
        except KeyError:
            return JSONResponse({"error": "unknown setup"}, status_code=404)

    async def gear(request: Request):
        if request.method == "POST":
            setups.set_gear(str((await request.json()).get("gear", "")))
        return JSONResponse({"gear": setups.get_gear()})

    # -- LIGHTING LAB -------------------------------------------------------
    async def lighting_plan(request: Request):
        body = await request.json()

        def work():
            refs = _refs(body)
            live = live_frame() if body.get("include_live") or not refs else None
            with assistant.feature("lighting lab"):
                return assistant.lighting_plan(refs, live, settings_view(studio.props()),
                                           str(body.get("request", ""))[:1000],
                                           "video" if body.get("mode") == "video" else "photo",
                                           gear=setups.get_gear() if body.get("use_gear", True) else "")
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except (assistant.AskError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def lighting_compare(request: Request):
        body = await request.json()

        def work():
            refs = _refs(body)
            if not refs:
                raise ValueError("Add a reference image first.")
            with assistant.feature("lighting lab"):
                return assistant.compare_to_reference(refs, live_frame(), str(body.get("title", ""))[:200])
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except (assistant.AskError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def index(_: Request):
        return HTMLResponse(PRO_PAGE.read_text(encoding="utf-8"))

    def catalog(_: Request):
        return JSONResponse(presets.catalog())

    PRESET_TO_PROFILE = {"portrait": "portrait", "lowlight": "lowlight", "landscape": "daylight", "product": "product",
                         "action": "daylight", "golden": "daylight", "bw": "portrait", "night": "lowlight",
                         "log": "interview", "log_hlg": "interview", "vlog": "interview", "cinematic": "interview",
                         "interview": "interview", "lowlight_video": "lowlight", "broll": "product", "slowmo": "daylight"}

    def profile_report(key: str, mode: str, by: str = "you", reason: str = "") -> dict:
        """Run a capture profile and return a report in the shape the Pro panel shows (looks never applied)."""
        frame = studio.frame
        before = round(presets.brightness(frame)[0]) if frame else 0
        e, v, exp = run_profile(key, mode)
        props = studio.props(max_age=0)
        prof = capture.PROFILES.get(key) or capture.PROFILES["interview" if mode == "video" else "portrait"]
        return {"preset": prof["name"], "look": None,
                "result": {k: label(k, props[PROPS[k]].value) for k in
                           ("exposure_mode", "aperture", "shutter_speed", "iso", "white_balance", "focus_mode") if PROPS[k] in props},
                "scene": {"before": before, "described": presets.describe(before), "after": exp.get("ire", 0) * 255 // 100,
                          "clipped_pct": exp.get("clip", 0)},
                "verified": sum(1 for i in v["items"] if i["status"] == "ok"),
                "mismatch": [i["text"] for i in v["items"] if i["key"] == "readback"],
                "warning": v["todo"], "tips": [i["todo"] for i in v["items"] if i["todo"]][:3],
                "verdict": v, "autopilot": {"by": by, "preset": key, "look": None, "reason": reason, "tip": ""}}

    async def preset(request: Request):
        """A capture profile (legacy preset keys are mapped onto profiles; looks are ignored, never baked in)."""
        body = await request.json()
        key = body.get("preset")
        key = key if key in capture.PROFILES else PRESET_TO_PROFILE.get(key)
        if not key:
            return JSONResponse({"error": "unknown profile"}, status_code=400)
        mode = body.get("mode") or (presets.PRESETS[body["preset"]].section if body.get("preset") in presets.PRESETS else None) \
            or autof.mode(_values())
        try:
            return JSONResponse(await anyio.to_thread.run_sync(lambda: profile_report(key, "video" if mode == "video" else "photo")))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def autopilot(request: Request):
        """One button: choose the capture profile for the scene and set up a clean, neutral capture."""
        body = await request.json()
        mode = "video" if body.get("mode") == "video" else "photo"
        intent = str(body.get("intent") or "auto")

        def work():
            if intent != "auto":
                key = intent if intent in capture.PROFILES else PRESET_TO_PROFILE.get(intent)
                if not key:
                    raise ValueError("unknown profile")
                return profile_report(key, mode)
            frame, _ = studio.latest_frame(-1, timeout=0)
            level = presets.brightness(frame)[0] if frame else 100
            key = "lowlight" if level < 60 else "interview" if mode == "video" else "portrait"
            return profile_report(key, mode, by="auto",
                                  reason="Dim light: widest aperture, lowest ISO the light allows." if key == "lowlight"
                                  else "Neutral capture for grading, exposure set by measurement.")
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def follow(request: Request):
        """Follow the light (video): GET = status, POST {on, over, max_iso, lock_iris}."""
        if request.method == "POST":
            body = await request.json()
            if body.get("on"):
                params = {k: body[k] for k in ("over", "lock_iris") if k in body}
                mi = _max_iso(body)
                if mi:
                    params["max_iso"] = mi
                follower.start(params)
            else:
                follower.stop()
        return JSONResponse({**follower.status, **follower.params})

    async def auto_start(request: Request):
        body = await request.json()
        try:
            auto.start(str(body.get("kind")), body.get("params") or {})
            return JSONResponse({"ok": True})
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # -- SPECIAL SHOTS: series shot by the camera, merged on the PC (originals kept, result beside them) --------
    series_state: dict = {"kind": None, "result": None}

    def _series_done(status: dict, ok: bool) -> None:
        import shutil as _sh
        from . import compute
        kind = series_state["kind"]
        names = [n for n in status.get("files", []) if n.lower().endswith(".jpg")]
        sh = Path(handoff.ensure_shoot()["path"])
        folder = sh / "SPECIAL" / f"{time.strftime('%H-%M-%S')} {compute.KINDS[kind]['name']}"
        folder.mkdir(parents=True, exist_ok=True)
        for n in status.get("files", []):                       # every original of the series, copied (never moved)
            src = CAPTURE_DIR / n
            if src.exists():
                _sh.copy2(src, folder / n)
        status["folder"] = str(folder)
        if len(names) < 2:
            status["result"] = None
            status["message"] = (status.get("message") or "") + " Not enough photos to merge."
            return
        status["message"] = "Merging…"
        res = compute.run(kind, [CAPTURE_DIR / n for n in names], folder / f"merged {compute.KINDS[kind]['name'].lower()}")
        preview = CAPTURE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{kind}.jpg"   # newest in the gallery = last take tile
        _sh.copy2(res["jpg"], preview)
        res["preview"] = preview.name
        res["first"] = names[0]
        res["partial"] = not ok
        status["result"] = res
        series_state["result"] = res
        status["message"] = f"{compute.KINDS[kind]['name']} ready" + (" (from the photos taken before it stopped)" if not ok else "") + "."

    async def series_start(request: Request):
        from . import compute
        body = await request.json()
        kind = str(body.get("kind"))
        if kind not in ("focusstack", "burst"):
            return JSONResponse({"error": "unknown special shot"}, status_code=400)
        if studio.recording:
            return JSONResponse({"error": "Not while recording."}, status_code=409)
        shots = int(body.get("shots") or compute.KINDS[kind]["shots"])
        params = {"shots": shots}
        if kind == "focusstack":
            params.update(step=int(body.get("step", 3)), near=int(body.get("near", 0)))
            if body.get("start") is not None and body.get("end") is not None:      # from "Check the object"
                params.update(start=int(body["start"]), end=int(body["end"]))
        series_state.update(kind=kind, result=None)
        try:
            auto.start(kind, params, on_done=_series_done)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        from .drivers.sony import S1_SETTLE_S
        return JSONResponse({"ok": True, "shots": shots, "seconds_per_photo": round(S1_SETTLE_S + 1.5, 1)})

    async def series_plan(_: Request):
        """"Check the object" for a focus stack (his press): step the focus near → far, measure where each part of
        the live picture is sharpest, put the focus back. No photo. Returns start/end and one plain verdict."""
        from . import stackplan
        if studio.recording:
            return JSONResponse({"error": "Not while recording."}, status_code=409)

        def work():
            before = focus_pos_now()
            if before is None:
                raise ValueError("This camera doesn't report its focus position, so the range can't be measured.")
            studio.run(lambda c: c.set("focus_mode", 1))
            for _ in range(15):
                studio.run(lambda c: c.focus_step(-7))
            time.sleep(1.0)
            samples = []
            for _ in range(22):
                f = fresh_frame(1, 2.0)
                p = focus_pos_now()
                if f:
                    samples.append((p, stackplan.tile_sharpness(f)))
                if p is None or p >= 70:
                    break
                studio.run(lambda c: c.focus_step(3))
                time.sleep(0.35)
            back = focus_goto_pos(before)
            frame = fresh_frame(1, 2.0)
            face = False
            try:
                from PIL import Image as _I
                face = bool(frame and (measure._faces_cv(_I.open(io.BytesIO(frame)).convert("RGB")) or []))
            except Exception:  # noqa: BLE001
                pass
            return {**stackplan.plan(samples, has_face=face), "focus_back": back, "focus_before": before}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def series_status(_: Request):
        st = dict(auto.status)
        st["kind"] = series_state["kind"]
        return JSONResponse(st)

    def series_cancel(_: Request):
        auto.stop()
        return JSONResponse({"ok": True})

    # -- MATCH THIS SHOT: a reference picture vs the live picture (local numbers, no Claude) ----------------
    match_state: dict = {"ref": None}

    async def match_ref(request: Request):
        from . import match
        body = await request.json()
        ref = str(body.get("ref") or "")
        if not ref:
            match_state["ref"] = None
            return JSONResponse({"ok": True, "ref": False})
        raw = ref.split(",", 1)[-1]
        if len(raw) > 8_000_000:
            return JSONResponse({"error": "Reference image too large"}, status_code=400)
        try:
            match_state["ref"] = match.prepare(base64.b64decode(raw))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Couldn't read that picture ({exc})"}, status_code=400)
        return JSONResponse({"ok": True, "ref": True})

    async def match_now(_: Request):
        from . import match
        if match_state["ref"] is None:
            return JSONResponse({"error": "Choose a reference picture first."}, status_code=400)

        def work():
            frame, _ = studio.latest_frame(-1, timeout=2.0)
            if frame is None:
                raise ValueError("No live picture yet.")
            pp = studio.props(max_age=5).get(presets.PROP_PICTURE_PROFILE)
            return match.compare(match_state["ref"], frame, log=bool(pp and camprofile.is_log(camprofile.of(studio), pp.value)))
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- CLAUDE PANEL: one home for every Claude action. Each press is exactly one request; a proposal first;
    #    nothing is applied without his Apply; no shoot or record ever. -------------------------------------
    CLAUDE_PROTECTED = {"picture_profile", "still_format", "movie_format", "movie_setting", "save_to", "exposure_mode"}
    claude_intent: dict = {}

    def _need_key():
        if not assistant.has_key():
            raise assistant.AskError("NO_KEY")

    def _claude_err(exc):
        msg = str(exc)
        if msg == "NO_KEY":
            msg = "Add your Claude key first: open the Claude panel and paste it there."
        return JSONResponse({"error": msg}, status_code=400)

    def _from_laptop(request: Request) -> bool:
        host = (request.client.host if request.client else "") or ""
        return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")

    def key_status() -> dict:
        from . import keystore
        src = assistant.key_source()
        prob = assistant.problem() if src else None
        return {"has_key": bool(src), "source": src, "since": keystore.saved_since() if src == "app" else None,
                "env_too": bool(assistant._env_key()), "last_ok": assistant.last_ok() if src else None,
                "problem": {"kind": prob["kind"], "text": prob["text"]} if prob else None}

    async def claude_state(request: Request):
        u = assistant.usage.today() if hasattr(assistant, "usage") else {"total": {"calls": 0}}
        return JSONResponse({**key_status(), "local": _from_laptop(request),
                             "calls": u.get("total", {}).get("calls", 0), "intent": dict(claude_intent)})

    from . import needs as _needs
    try:
        NEEDS = _needs.check()                         # one list: requirements.txt (same as "Install or repair")
    except Exception:  # noqa: BLE001
        NEEDS = {"missing_required": [], "missing_optional": []}
    MISSING = [e["package"] for e in NEEDS["missing_required"]]
    for e_ in NEEDS["missing_required"] + NEEDS["missing_optional"]:
        print(_needs.sentence(e_), flush=True)

    ready_cache: dict = {}

    async def ready_view(request: Request):
        """Getting ready: is the camera there, is a Claude key saved? Never calls Claude (only checks a key is saved)."""
        h = studio.health()
        cam_ok = h.get("state") == "ok" and studio.cam is not None
        saved = assistant.has_key()
        prob = assistant.problem() if saved else None
        from .drivers import base as drvbase
        prof = camprofile.of(studio) if cam_ok else {}
        model = prof.get("model") if cam_ok else None
        other = []
        if not cam_ok:
            now = time.time()
            if now - ready_cache.get("t", 0) > 10:                 # pnputil is slow: at most every 10 s
                try:
                    from . import ptpusb
                    ready_cache.update(t=now, usb=[c for c in ptpusb.cameras_on_usb() if c["brand"] != "Sony"])
                except Exception:  # noqa: BLE001
                    ready_cache.update(t=now, usb=[])
            other = ready_cache.get("usb") or []
        usb0 = None
        if other:
            try:
                from .drivers import registry as _reg
                usb0 = _reg.lookup_usb(int(other[0]["vendor_id"], 16), int(other[0]["product_id"], 16))
            except Exception:  # noqa: BLE001
                usb0 = None
        g = (drvbase.guide(other[0]["brand"], (usb0 or {}).get("model"), False, usb=usb0) if other else
             drvbase.guide(getattr(studio.cam, "brand", "Sony"), model, cam_ok) if cam_ok else drvbase.guide("Sony", "ZV-E10", False))
        text = (f"Connected: {studio.cam.name}" if cam_ok and studio.cam else
                (g["text"] if other else h.get("message") or "Looking for the camera on the USB cable and on Wi-Fi…"))
        return JSONResponse({
            "camera": {"ok": cam_ok, "text": text, "guide": g, "other_brand": other[0] if other else None},
            "claude": {"saved": saved, "source": assistant.key_source(), "local": _from_laptop(request),
                       "problem": {"kind": prob["kind"], "text": prob["text"]} if prob else None},
            "libs": {"missing": MISSING,                      # required: core features break, so the card says it
                     "text": " ".join(_needs.sentence(e_) for e_ in NEEDS["missing_required"]),
                     "optional": [_needs.sentence(e_) for e_ in NEEDS["missing_optional"]]},   # never brings the card up
            "show": (not cam_ok or not saved or prob is not None or bool(MISSING)
                     or (cam_ok and g.get("kind") == "untested"))})

    async def claude_key(request: Request):
        """The Claude key, entered in Studio: only from the laptop itself; never sent back, never logged."""
        from . import keystore
        local = _from_laptop(request)
        if request.method == "GET":
            ks = key_status()
            return JSONResponse({"set": ks["has_key"], **ks, "local": local})
        if not local:
            return JSONResponse({"error": "Add the key on the laptop."}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("action") == "remove":
            keystore.remove()
            src = assistant.key_source()
            return JSONResponse({"ok": True, "set": bool(src), "source": src,
                                 "message": "Removed from Studio." + (" Studio now uses the key set in Windows." if src == "env" else
                                                                      " Claude is off until you add a key.")})
        key = str(body.get("key", "")).strip()
        body = None
        if not keystore.looks_like_a_key(key):
            return JSONResponse({"error": "That doesn't look like a Claude key. It starts with sk-ant-."}, status_code=400)
        had = bool(assistant.key_source())
        try:
            status, msg = await anyio.to_thread.run_sync(lambda: assistant.test_key(key))
            if status not in ("ok", "nocredit"):        # only a key that passed the test is stored
                keep = " The new key was not saved; the old key is still in use." if had else " The key was not saved."
                return JSONResponse({"error": msg + keep, "status": status}, status_code=400)
            await anyio.to_thread.run_sync(lambda: keystore.save(key))
        except Exception:  # noqa: BLE001 - never echo anything that could contain the key
            return JSONResponse({"error": "Studio couldn't save the key on this laptop."}, status_code=500)
        finally:
            key = ""
        if status in ("ok", "nocredit"):
            assistant.set_problem(None if status == "ok" else "nocredit")
        text = ("New key saved and working. " if had else "Key saved. ") + msg
        return JSONResponse({"ok": True, "status": status, "message": text, "set": True, "source": "app"})

    async def claude_direct(request: Request):
        body = await request.json()
        req = str(body.get("request", "")).strip()[:600]
        mode = "video" if body.get("mode") == "video" else "photo"

        def work():
            _need_key()
            jpg = live_frame()
            view = [s for s in settings_view(studio.props(max_age=0)) if s["name"] not in CLAUDE_PROTECTED]
            with assistant.feature("claude: direct"):
                r = assistant.ask(jpg, view, (req or "Direct this shot: the best professional result for this scene.")
                                  + f" Mode: {mode}." + post_goal() + " Explain in two or three plain sentences.")
            ch = [c for c in r.get("changes", []) if c["name"] not in CLAUDE_PROTECTED]
            return {"kind": "direct", "say": r.get("explanation", ""), "tips": r.get("tips", ""), "changes": ch}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except assistant.AskError as exc:
            return _claude_err(exc)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def claude_review(_: Request):
        def work():
            _need_key()
            sh = handoff.current()
            items = importer.takes(sh) if sh else []
            if not items:
                return {"kind": "review", "say": "There is no take in this shoot yet. Take a photo or record a clip, then ask again. Nothing was sent to Claude.", "changes": []}
            tid = items[-1]["id"]
            r = _claude_review(tid)
            say = " ".join(list(r.get("good", []))[:2] + list(r.get("fix", []))[:3])
            ch = [c for c in r.get("changes", []) if c["name"] not in CLAUDE_PROTECTED]
            return {"kind": "review", "take": tid, "say": say, "good": r.get("good", []), "fix": r.get("fix", []), "changes": ch}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except assistant.AskError as exc:
            return _claude_err(exc)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def claude_best(_: Request):
        from . import bestpick

        def work():
            sh = handoff.current()
            items = importer.takes(sh) if sh else []
            kind = _situation()["mode"]                      # Photo: the best photo; Video: the best take
            items = [t for t in items if t.get("kind", "photo") == kind]
            folder = Path(sh["path"]) / "NOTES" / "takes" if sh else None
            frames = []
            for t in items[-24:]:
                f = folder / f"take{t['id']:03d}.jpg" if folder else None
                if f is not None and f.exists():
                    frames.append((t["id"], f.read_bytes()))
            if len(frames) < 2:
                return {"kind": "best", "say": "There are fewer than two takes in this shoot, so there is nothing to choose from yet. Nothing was sent to Claude.",
                        "changes": [], "best": [], "ranked": []}
            ranked = bestpick.rank(frames)                       # local and free: sharpness, eyes open
            _need_key()
            top = ranked[:4]
            jpg = dict(frames)
            sound = audiocheck.load(Path(sh["path"])) if sh else {}     # the sound numbers, when the clip was imported
            for r in top:
                t = next((x for x in items if x["id"] == r["id"]), {})
                rep = next((sound[Path(f).name] for f in (t.get("files") or []) if Path(f).name in sound), None)
                if rep and rep.get("summary"):
                    r["words"] = f"{r.get('words', '')}; {rep['summary']}".strip("; ")
            with assistant.feature("claude: best take"):
                pick = bestpick.ask_claude([(r["id"], jpg[r["id"]], r) for r in top])
            for tid in pick["best"]:
                try:
                    importer.update_take(tid, best=True)
                except Exception:  # noqa: BLE001
                    pass
            names = " and ".join(f"take {i}" for i in pick["best"])
            return {"kind": "best", "say": f"Best: {names}. {pick['why']} Every take is kept; nothing was deleted.",
                    "best": pick["best"], "ranked": [{k: r[k] for k in ("id", "score", "words")} for r in ranked[:6]], "changes": []}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except assistant.AskError as exc:
            return _claude_err(exc)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def claude_apply(request: Request):
        body = await request.json()
        changes = [{"name": str(c.get("name")), "value": int(c.get("value"))} for c in body.get("changes", [])
                   if c.get("name") in CONTROLS and c.get("name") not in CLAUDE_PROTECTED]
        say = str(body.get("say", ""))[:300]
        if not changes:
            return JSONResponse({"error": "Nothing to apply."}, status_code=400)
        if studio.recording:
            return JSONResponse({"error": "Claude waits until the take is finished."}, status_code=409)

        def work():
            autof.studio_busy(20)
            follower.pause(15)
            r = changelog.apply(changes, "claude", say or "Claude")
            now = {s["name"]: s["value"] for s in settings_view(studio.props(max_age=0))}
            ok = [c["name"] for c in changes if now.get(c["name"]) == c["value"]]          # read back from the camera
            for n in ok:
                autof.manual(n)                                  # his choice now: the Operator does not argue with it
            claude_intent.clear()
            if ok:
                claude_intent.update(values={c["name"]: c["value"] for c in changes if c["name"] in ok}, say=say, t=time.time())
            return {**r, "verified": ok, "mismatch": [c["name"] for c in changes if c["name"] not in ok],
                    "values": claude_intent.get("values", {})}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    # -- ZOOM: the power zoom of his kit lens. Only on his press (hold or a stop); the Operator never zooms. ---------------
    from .drivers.sony import zoom_mm, zoom_pos_for
    zoom_state: dict = {"moving": 0, "since": 0.0, "before": None, "result": None, "busy": False}

    def _mm(pos):
        """Millimetres only with a calibration stored for this model and lens; otherwise the position alone."""
        if pos is None or not (camprofile.of(studio).get("lens") or {}).get("zoom_mm"):
            return None
        return round(zoom_mm(pos))

    def _zoom_read(c):
        props = c.refresh()
        zp = props.get(SP_ZOOM_POSITION)
        ap = props.get(PROPS["aperture"])
        pos = (zp.value & 0xFF) if zp is not None and isinstance(zp.value, int) else None
        return pos, (ap.value / 100 if ap is not None and isinstance(ap.value, int) else None)

    def _zoom_begin():
        if zoom_state["before"] is None:
            pos, f = studio.run(_zoom_read)
            zoom_state["before"] = {"pos": pos, "f": f}
            if zoom_state.get("last_f") != f:               # he set this aperture himself since the last zoom
                zoom_state["chosen_f"] = f
        autof.studio_busy(15)

    def _zoom_finish():
        time.sleep(0.5)
        pos, f = studio.run(_zoom_read)
        b = zoom_state["before"] or {}
        zoom_state["before"] = None
        stops = round(2 * math.log2(f / b["f"]), 2) if f and b.get("f") else 0.0
        zoom_state["last_f"] = f
        res = {"pos": pos, "mm": _mm(pos), "f": f, "f_before": b.get("f"), "f_chosen": zoom_state.get("chosen_f"),
               "pos_before": b.get("pos"), "darker_stops": stops, "t": time.time()}
        zoom_state["result"] = res
        if pos != b.get("pos"):
            autof.manual("aperture")                     # the lens changed it: his choice, not "the light changed"
            focus_state.update(text="not checked after the zoom", point=None, tracking=False, t=time.time(), found=None)
        return res

    def _zoom_watchdog(since: float):
        time.sleep(6.0)                                   # a lost "stop" never leaves the lens running
        if zoom_state["moving"] and zoom_state["since"] == since:
            try:
                studio.run(lambda c: c.zoom(0))
            finally:
                zoom_state["moving"] = 0
                _zoom_finish()

    def zoom_goto_pos(target: int):
        """Drive the power zoom to a position (0 wide .. 100 tele) and report where it landed. Stop on the position
        report with a lead for the lens's run-on, then small corrections. Tried 12 Nov 2026: coming only from the wide
        side without corrections overshot by 5-8 mm, so this stays; the real landing is always reported."""
        zoom_state["busy"] = True
        try:
            _zoom_begin()
            for attempt in range(3):
                pos, _f = studio.run(_zoom_read)
                gap = target - pos
                if abs(gap) <= (3 if target not in (0, 100) else 0):
                    break
                d = 1 if gap > 0 else -1
                lead = 0 if target in (0, 100) else (8 if attempt == 0 else 2)
                studio.run(lambda c: c.zoom(d))
                t_end = time.time() + 4.0
                try:
                    if attempt and target not in (0, 100):
                        time.sleep(0.12)
                    else:
                        while time.time() < t_end:
                            p, _f = studio.run(_zoom_read)
                            if p is not None and (p - (target - d * lead)) * d >= 0:
                                break
                            time.sleep(0.05)
                finally:
                    studio.run(lambda c: c.zoom(0))
                time.sleep(0.6)
            return _zoom_finish()
        finally:
            zoom_state["busy"] = False

    async def zoom_view(request: Request):
        if request.method == "GET":
            def read():
                if zoom_state["moving"] or zoom_state["busy"]:
                    pos, f = studio.run(_zoom_read)          # live while the lens moves
                else:                                       # idle: the shared settings read, no extra camera traffic
                    props = studio.props(max_age=2)
                    zp, ap = props.get(SP_ZOOM_POSITION), props.get(PROPS["aperture"])
                    pos = (zp.value & 0xFF) if zp is not None and isinstance(zp.value, int) else None
                    f = ap.value / 100 if ap is not None and isinstance(ap.value, int) else None
                return {"available": pos is not None, "pos": pos, "mm": _mm(pos),
                        "f": f, "moving": zoom_state["moving"], "busy": zoom_state["busy"], "recording": studio.recording,
                        "last": zoom_state["result"]}
            try:
                return JSONResponse(await anyio.to_thread.run_sync(read))
            except Exception as exc:  # noqa: BLE001
                return err(exc)
        body = await request.json()
        action = str(body.get("action", ""))
        if action in ("start", "goto") and (getattr(studio, "setup_active", 0) > 0 or autof.phase == "deciding"):
            # verified 12 Nov 2026: a zoom during the setup on connect made its exposure loop stop down to f/22
            return JSONResponse({"error": "Studio is still setting up the camera. Zoom again in a moment."}, status_code=409)

        def start(d):
            _zoom_begin()
            studio.run(lambda c: c.zoom(d))
            zoom_state.update(moving=d, since=time.time())
            threading.Thread(target=_zoom_watchdog, args=(zoom_state["since"],), daemon=True).start()
            return {"ok": True, "moving": d}

        def stop():
            studio.run(lambda c: c.zoom(0))
            was = zoom_state["moving"]
            zoom_state["moving"] = 0
            return {"ok": True, "result": _zoom_finish() if was or zoom_state["before"] else zoom_state["result"]}

        def goto(mm):
            return zoom_goto_pos(zoom_pos_for(mm))

        try:
            if action == "start":
                d = 1 if int(body.get("dir", 1)) > 0 else -1
                return JSONResponse(await anyio.to_thread.run_sync(lambda: start(d)))
            if action == "stop":
                return JSONResponse(await anyio.to_thread.run_sync(stop))
            if action == "goto":
                if not (camprofile.of(studio).get("lens") or {}).get("zoom_mm"):
                    return JSONResponse({"error": "No millimetre calibration for this lens yet: hold Wider or Closer."}, status_code=400)
                if zoom_state["busy"]:
                    return JSONResponse({"error": "The lens is still moving."}, status_code=409)
                return JSONResponse({"ok": True, "result": await anyio.to_thread.run_sync(lambda: goto(float(body.get("mm", 16))))})
            return JSONResponse({"error": "unknown zoom action"}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            try:
                studio.run(lambda c: c.zoom(0))
            except Exception:  # noqa: BLE001
                pass
            zoom_state.update(moving=0, busy=False)
            return err(exc, 400)

    # -- CHECK THIS CAMERA: an untested model is tested without shooting; results are saved per model ---------------
    check_state: dict = {"running": False, "steps": [], "log_candidates": [], "done": None, "model": None}

    def _check_camera():
        steps = []
        check_state.update(running=True, steps=steps, log_candidates=[], done=None)
        prof = camprofile.of(studio)
        model = prof.get("model")
        check_state["model"] = model

        t_last = [time.time()]

        def step(name, ok, detail="", command=""):
            now = time.time()
            steps.append({"name": name, "ok": ok, "detail": detail, "ms": int((now - t_last[0]) * 1000), "command": command})
            t_last[0] = now
        try:
            props = studio.props(max_age=0)
            step("Connected", True, studio.cam.name if studio.cam else "")
            seq = studio.frame_seq
            f, s2 = studio.latest_frame(seq, timeout=4)
            step("Live picture", f is not None and s2 > seq, f"{studio.fps:.0f} frames per second" if f else "no picture")
            for name in ("aperture", "shutter_speed", "iso", "white_balance", "focus_mode"):
                code = code_of(name)
                p = props.get(code)
                nice = {"aperture": "Aperture", "shutter_speed": "Shutter", "iso": "ISO", "white_balance": "White balance",
                        "focus_mode": "Focus mode"}[name]
                if p is None:
                    step(nice, None, "not reported by this camera")
                    continue
                ch = [c for c in (p.choices or []) if c != p.value and c not in (0, 0xFFFFFF)]
                if not (p.writable and p.enabled) or not ch:
                    step(nice, None, "read only in this mode")
                    continue
                orig = p.value
                other = min(ch, key=lambda c: abs((c - orig) if isinstance(c, int) else 0))   # the nearest listed value
                try:
                    studio.run(lambda c, code=code, v=other: c.set(code, v))
                    back = studio.props(max_age=0)[code].value
                finally:
                    studio.run(lambda c, code=code, v=orig: c.set(code, v))
                restored = studio.props(max_age=0)[code].value
                step(nice, back == other and restored == orig,
                     f"{label(name, orig)} → {label(name, other)} → {label(name, restored)}",
                     f"SetDevicePropValue 0x{code:04X} = {other}, read back {back}, restored {restored}")
            fm = studio.props(max_age=0).get(PROPS["focus_mode"])
            try:
                studio.run(lambda c: c.autofocus(hold=0.6))        # a half press only, never the shutter
                step("Half-press (focus)", True, "answered", "Control 0xD2C1 press/release")
            except Exception as exc:  # noqa: BLE001
                step("Half-press (focus)", False, str(exc)[:120])
            if fm is not None and studio.props(max_age=0)[PROPS["focus_mode"]].value != fm.value:
                studio.run(lambda c: c.set(PROPS["focus_mode"], fm.value))
            zp = studio.props(max_age=0).get(SP_ZOOM_POSITION)
            if zp is None:
                step("Power zoom", None, "not reported (no power zoom lens)")
            else:
                p0 = zp.value & 0xFF
                studio.run(lambda c: c.zoom(1)); time.sleep(0.3); studio.run(lambda c: c.zoom(0)); time.sleep(0.8)
                p1 = studio.props(max_age=0)[SP_ZOOM_POSITION].value & 0xFF
                end = time.time() + 5
                while (studio.props(max_age=0)[SP_ZOOM_POSITION].value & 0xFF) > p0 and time.time() < end:
                    studio.run(lambda c: c.zoom(-1)); time.sleep(0.25); studio.run(lambda c: c.zoom(0)); time.sleep(0.4)
                step("Power zoom", p1 != p0, f"position {p0} → {p1} → {studio.props(max_age=0)[SP_ZOOM_POSITION].value & 0xFF}")
            pp = studio.props(max_age=0).get(presets.PROP_PICTURE_PROFILE)
            if pp is None or not pp.choices:
                step("Log profile search", None, "no picture profiles on this camera")
            else:
                orig, cands, ref = pp.value, [], None
                try:
                    for v in ([0] if 0 in pp.choices else []) + [c for c in pp.choices if c != 0]:
                        try:
                            studio.run(lambda c, v=v: c.set(presets.PROP_PICTURE_PROFILE, v))
                        except Exception:  # noqa: BLE001
                            continue
                        time.sleep(1.4)
                        fr, _s = studio.latest_frame(studio.frame_seq, timeout=2)
                        if fr:
                            m = camprofile.looks_like_log(fr)
                            if v == 0:
                                ref = m                         # the camera's normal picture, to compare with
                            else:
                                cands.append({"pp": v, **m})
                finally:
                    studio.run(lambda c: c.set(presets.PROP_PICTURE_PROFILE, orig))
                if ref:                                          # clearly flatter than the normal picture, blacks lifted
                    flat = [c for c in cands if c["black"] >= ref["black"] + 8 and c["contrast"] <= max(ref["contrast"] * 0.85, ref["contrast"] - 10)]
                else:
                    flat = [c for c in cands if c["log_like"]]
                logs = sorted(flat, key=lambda c: (c["contrast"], c["saturation"]))
                check_state["log_candidates"] = logs
                step("Log profile search", True, ("looks like log: " + ", ".join(f"PP{c['pp']}" for c in logs)) if logs
                     else "no picture profile looked like log; picture profile left as it was")
        except Exception as exc:  # noqa: BLE001
            step("Stopped", False, str(exc)[:160])
        finally:
            core = [s for s in steps if s["name"] in ("Connected", "Live picture", "Aperture", "Shutter", "ISO",
                                                       "White balance", "Focus mode", "Half-press (focus)")]
            ok = bool(core) and all(s["ok"] is not False for s in core) and any(s["ok"] for s in core)
            camprofile.save_result(model, tested=ok, check={"t": time.time(), "steps": steps})
            studio._profile = None                               # read the saved result next time
            check_state.update(running=False, done=time.time(), passed=ok)

    def _report_key(prof: dict, fw: str) -> str:
        return f"{prof.get('brand') or 'Sony'}|{prof.get('model')}|{fw}"

    async def camera_report(request: Request):
        """"Save report" / "Share report": build the camera report (nothing personal), save it, and give the issue
        page address. Studio sends nothing: the user opens the page and submits it himself."""
        from . import report

        def work():
            prof = camprofile.of(studio)
            try:
                di = studio.run(lambda c: c.device_info()) if hasattr(studio.cam, "device_info") else None
            except Exception:  # noqa: BLE001
                di = None
            props = studio.props(max_age=0) if studio.cam else {}
            other = (ready_cache.get("usb") or [None])[0] if studio.cam is None else None
            from .drivers import base as drvbase
            brand = other["brand"] if other else getattr(studio.cam, "brand", None) or "Sony"
            checks = list(check_state.get("steps") or []) if not other else [
                {"name": "Connect", "ok": False, "ms": 0, "command": "",
                 "detail": (f"Studio could not open the {brand} camera (untested driver; on Windows the camera needs the "
                            "WinUSB driver, which Studio never installs)") if drvbase.driver_ok(brand) else
                 f"Studio has no driver for {brand} cameras yet"}]
            fm = None
            if studio.cam is not None:
                try:
                    rows = _format_rows()
                    fm = {m: [{"title": r["title"], "offered": [c["label"] for c in r["choices"]] or [r["value_name"]],
                               "now": r["value_name"], "chosen": r["chosen_name"], "settable": r["settable"]}
                              for r in rows[m]] for m in ("photo", "video")}
                except Exception:  # noqa: BLE001
                    fm = None
            full, summary = report.build(brand, di, props, checks, "Wi-Fi" if studio.host not in ("usb", None) else "USB",
                                         usb=other, formats=fm)
            if not full.get("model"):
                full["model"] = prof.get("model") or ""
            j, t = report.save(setups.BASE / "reports", full, summary)
            url, too_long = report.issue_link(full, summary)
            return {"summary": summary, "json": str(j), "txt": str(t), "issue_url": url, "too_long": too_long,
                    "removed": "Removed: serial number, names, network details.", "file_name": j.name}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def camera_report_open(request: Request):
        """Open the reports folder with the file selected (laptop only)."""
        if not _from_laptop(request):
            return JSONResponse({"error": "Only on the laptop."}, status_code=403)
        name = str((await request.json()).get("file", ""))
        f = (setups.BASE / "reports" / Path(name).name)
        if not f.exists():
            return JSONResponse({"error": "The report file is not there any more."}, status_code=404)
        subprocess.Popen(["explorer", "/select,", str(f)])
        return JSONResponse({"ok": True})

    async def camera_report_notnow(request: Request):
        """He said "Not now": never ask again for this brand, model and firmware."""
        key = str((await request.json()).get("key", ""))[:120]
        asked = prefs().get("report_asked") or []
        if key and key not in asked:
            save_prefs(report_asked=asked + [key])
        return JSONResponse({"ok": True})

    async def camera_check(request: Request):
        if request.method == "POST":
            if check_state["running"]:
                return JSONResponse({"error": "The check is already running."}, status_code=409)
            if studio.recording:
                return JSONResponse({"error": "Not while recording."}, status_code=409)
            threading.Thread(target=_check_camera, daemon=True).start()
            return JSONResponse({"ok": True})
        prof = camprofile.of(studio)
        key = _report_key(prof, "")
        ask = bool(check_state.get("done")) and not prof.get("builtin") and key not in (prefs().get("report_asked") or [])
        return JSONResponse({**{k: v for k, v in check_state.items()}, "ask_share": ask, "report_key": key,
                             "profile": {k: v for k, v in prof.items() if k in ("model", "tested", "log_pp", "log_confirmed", "builtin")}})

    async def camera_log(request: Request):
        """He confirms which picture profile is his log profile (saved for this model)."""
        body = await request.json()
        prof = camprofile.of(studio)
        if body.get("pp") is None:
            camprofile.save_result(prof["model"], log_pp=None, log_confirmed=False)
        else:
            camprofile.save_result(prof["model"], log_pp=int(body["pp"]), log_confirmed=True, log_kind=None)
        studio._profile = None
        return JSONResponse({"ok": True, "profile": {k: v for k, v in camprofile.of(studio).items() if k in ("model", "log_pp", "log_confirmed")}})

    def auto_stop(_: Request):
        auto.stop()
        return JSONResponse({"ok": True})

    def auto_status(_: Request):
        return JSONResponse(auto.status)

    def all_props(_: Request):
        """Every property the camera reports (for the All settings panel)."""
        try:
            props = studio.run(lambda c: c.refresh())
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        by_code = {code_of(n): n for n in CONTROLS}
        out = []
        for code, p in sorted(props.items()):
            name = by_code.get(code)
            lab = (lambda v: label(name, v)) if name else str
            out.append({"code": f"0x{code:04X}", "name": KNOWN_PROPS.get(code, f"0x{code:04X}"),
                        "value": p.value, "label": lab(p.value), "writable": p.writable, "enabled": p.enabled,
                        "choices": [{"value": c, "label": lab(c)} for c in p.choices],
                        "range": list(p.range) if p.range else None})
        return JSONResponse(out)

    async def set_prop(request: Request):
        body = await request.json()
        try:
            code, value = int(str(body["code"]), 16), int(body["value"])
        except (KeyError, ValueError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        if code == SP_SHOOT_CLASS:
            return JSONResponse({"error": "This one follows the shooting mode; change the mode instead."}, status_code=400)
        for n in CONTROLS:
            if code_of(n) == code:
                autof.manual(n)
        try:
            await anyio.to_thread.run_sync(lambda: studio.run(lambda c: c.set(code, value)))
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    def lut_export(request: Request):
        key = request.path_params["key"]
        source = request.query_params.get("input", "rec709")
        if source not in lut.INPUTS:
            return Response(status_code=400)
        if key == "log709":                      # plain technical LUT
            if source == "rec709":
                return Response(status_code=400)
            text = lut.export_cube(None, f"camctl {lut.INPUTS[source]} to Rec709", source=source)
            fname = f"camctl_{source}_to_rec709.cube"
        else:                                    # looks have one home now (More › Looks): no old look LUTs here
            return Response(status_code=404)
        return Response(text, media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    REC_EVIDENCE_S = 6.0

    def record_state(_: Request):
        """The recording state. Studio keeps its OWN intent (what it last started or stopped). A camera flag that
        disagrees is never enough by itself (12 Nov 2026 it read "not recording" 8 s into a take): Studio only
        follows the camera when the flag disagrees steadily for 6 s AND the card's remaining record time agrees
        (counting down = recording, standing still = not). Until then the state is "checking" and no start is taken."""
        if card_rec["state"] in ("starting", "recording", "finishing") or getattr(studio, "card_recording", False):
            # recording on the laptop (capture card): the camera's own flag is not about this recording
            return JSONResponse({"recording": card_rec["state"] == "recording", "checking": False,
                                 "note": card_rec["say"], "laptop": card_rec["state"],
                                 "elapsed": time.time() - card_rec["t0"] if card_rec["state"] == "recording" else 0})
        try:
            props = studio.props(max_age=0)
            st = status_view(props)
            left = _num(props, PROP_REC_TIME_LEFT)
        except Exception:  # noqa: BLE001
            st, left = {"recording": None}, None
        flag = st.get("recording")
        now = time.time()
        if flag is None or flag == studio.recording or now - studio.rec_toggled < 3:
            studio.rec_check = None
        else:
            chk = studio.rec_check
            if chk is None or chk.get("flag") != flag:
                studio.rec_check = chk = {"since": now, "left0": left, "flag": flag}
            elif now - chk["since"] >= REC_EVIDENCE_S and left is not None and chk["left0"] is not None:
                counting = chk["left0"] - left >= 2              # the card is filling: really recording
                if counting == flag:                              # flag and card agree: follow the camera
                    studio.recording = flag
                    studio.rec_since = now if flag else 0.0
                    studio.rec_check = None
                    msg = ("Recording was started on the camera itself." if flag else
                           "The camera stopped recording by itself (card full, too warm or battery?). The take is on the card.")
                    studio.rec_note = msg
                    from .drivers.sony import _clog
                    _clog("movie state from camera", recording=int(flag), left=left, why="flag and card agree for 6 s")
                    autof._event(msg, kind="camera")
                else:
                    studio.rec_check = {"since": now, "left0": left, "flag": flag}   # flag and card disagree: keep waiting
        st["recording"] = studio.recording
        st["checking"] = studio.rec_check is not None
        st["note"] = getattr(studio, "rec_note", "")
        st["elapsed"] = now - studio.rec_since if studio.recording else 0
        return JSONResponse(st)

    def liveview(request: Request):
        try:
            after = int(request.query_params.get("after", "-1"))
        except ValueError:
            after = -1
        jpg, seq = studio.latest_frame(after)
        if jpg is None or (studio.frame_error and seq <= after):
            h = studio.health()
            return JSONResponse({"error": studio.frame_error or "no live view yet", "health": h}, status_code=503)
        return Response(jpg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Frame-Seq": str(seq), "X-Frame-T": f"{getattr(studio, 'frame_t', 0):.3f}",
                                 "X-FPS": f"{studio.fps:.1f}"})

    # -- PLAYGROUND › DAY REEL: the moment before each photo, from the live picture (memory until a photo) ---------
    studio.reel = reel.Buffer()
    try:
        _sh0 = handoff.current()
        studio.reel.set_on(reel.pref(Path(_sh0["path"])) if _sh0 else False)
    except Exception:  # noqa: BLE001
        pass
    reel_job: dict = {"busy": False, "step": 0, "of": 0, "message": "", "out": None, "stop": threading.Event()}

    def _reel_clip_later(frames, photos):
        sh = handoff.current()
        if not sh:
            return

        def work():
            try:
                reel.write_clip(frames, Path(sh["path"]), photos)
            except Exception as exc:  # noqa: BLE001 - a missing moment never touches the photo
                autof._event("The moment before this photo could not be saved: " +
                             ("the disk is full." if getattr(exc, "errno", None) == 28 else "Studio's video tool is missing."
                              if "ffmpeg" in str(exc).lower() else "something went wrong while writing it."), kind="note")
        threading.Thread(target=work, daemon=True, name="reel-clip").start()

    def _reel_state() -> dict:
        sh = handoff.current()
        folder = Path(sh["path"]) if sh else None
        want = reel.pref(folder) if folder else False         # remembered per shoot: a new shoot starts off
        if want != studio.reel.on:
            studio.reel.set_on(want)
        n = len(reel.today(folder)) if folder else 0
        size = reel.size_today(folder) if folder else 0
        return {"on": studio.reel.on, "paused": bool(studio.recording) or studio.cam is None, "moments": n,
                "size_mb": round(size / 1e6, 1), "shoot": bool(folder and folder.exists()),
                "job": {k: v for k, v in reel_job.items() if k != "stop"},
                "folder": str(reel.folder(folder)) if folder else None}

    async def reel_view(request: Request):
        """GET the day reel's state; POST {on} his switch (remembered per shoot; off = buffer emptied at once)."""
        if request.method == "POST":
            body = await request.json()
            sh = handoff.ensure_shoot()
            on = bool(body.get("on"))
            reel.save_pref(Path(sh["path"]), on)
            studio.reel.set_on(on)
            autof._event("You switched the day reel " + ("on: Studio keeps the last 3 seconds of the live picture"
                                                         if on else "off: the kept frames were dropped"), kind="you")
        return JSONResponse(_reel_state())

    async def reel_make(request: Request):
        """"Make today's reel": in the background, with a progress line and Stop. Reads clips and photos only."""
        sh = handoff.current()
        if not sh or not Path(sh["path"]).exists():
            return JSONResponse({"error": "The shoot folder is missing, so there is nothing to make a reel from."}, status_code=400)
        if reel_job["busy"]:
            return JSONResponse(_reel_state())
        if video.find_ffmpeg() is None:
            return JSONResponse({"error": "Studio's video tool (ffmpeg) is missing, so the reel can't be made. "
                                          "Run \"Install or repair camctl Studio\" once."}, status_code=400)
        folder = Path(sh["path"])
        if not reel.today(folder):
            return JSONResponse({"error": "No moments yet today. Switch the day reel on, then take photos with Studio."},
                                status_code=400)
        reel_job.update(busy=True, step=0, of=0, message="Making today's reel…", out=None)
        reel_job["stop"].clear()

        def prog(i, n):
            reel_job.update(step=i, of=n, message=f"Making today's reel… {i} of {n}")

        def work():
            try:
                out = reel.make(folder, reel_job["stop"], prog)
                reel_job.update(out=str(out), message=f"Done: {out.name} is in the REEL folder of your shoot.")
            except InterruptedError:
                reel_job.update(message="Stopped. Nothing was changed; your photos and clips are as they were.")
            except OSError as exc:
                reel_job.update(message="The disk is full, so the reel could not be saved. Free some space and try again."
                                if getattr(exc, "errno", None) == 28 else "The reel could not be saved in the shoot folder.")
            except Exception:  # noqa: BLE001
                reel_job.update(message="The reel could not be made this time. Your photos and clips are unchanged.")
            finally:
                reel_job["busy"] = False
        threading.Thread(target=work, daemon=True, name="day-reel").start()
        return JSONResponse(_reel_state())

    def reel_stop(_: Request):
        reel_job["stop"].set()
        return JSONResponse(_reel_state())

    async def reel_delete(request: Request):
        """Today's CLIPS only, after his confirm naming the count; never photos, never the reel."""
        body = await request.json()
        sh = handoff.current()
        if not sh:
            return JSONResponse({"error": "There is no shoot open."}, status_code=400)
        folder = Path(sh["path"])
        n = len(reel.today(folder))
        if int(body.get("count", -1)) != n:
            return JSONResponse({"error": f"Today has {n} clips now. Press Delete again to confirm that number."},
                                status_code=409)
        gone = reel.delete_today(folder)
        autof._event(f"You deleted today's {gone} reel clips (the photos are kept)", kind="you")
        return JSONResponse({**_reel_state(), "deleted": gone})

    def reel_show(request: Request):
        """Opens the REEL folder in Explorer: laptop only."""
        if not (request.client and (request.client.host or "").startswith("127.")):
            return JSONResponse({"error": "Open the folder on the laptop."}, status_code=403)
        sh = handoff.current()
        if not sh:
            return JSONResponse({"error": "There is no shoot open."}, status_code=400)
        f = reel.folder(Path(sh["path"]))
        f.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(f))  # noqa: S606 - his press: show the folder
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Windows didn't open the folder."}, status_code=500)
        return JSONResponse({"ok": True})

    # -- LOOKS (More › Looks): by his press only; copies of finished files, and a marked live preview in the page ----
    look_job: dict = {"busy": False, "message": "", "out": None, "stop": threading.Event()}

    def _shoot_file(rel: str) -> Path:
        """A finished photo or clip of the CURRENT shoot (nothing outside it, never the card)."""
        sh = handoff.current()
        if not sh:
            raise ValueError("There is no shoot open.")
        root = Path(sh["path"]).resolve()
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", *importer.VIDEO_EXT):
            raise ValueError("Choose a photo or clip from this shoot.")
        if "LOOKS" in p.relative_to(root).parts:
            raise ValueError("Choose an original photo or clip, not a copy with a look.")
        return p

    def _clip_is_log(p: Path) -> bool:
        for t in importer.takes(handoff.current()):
            if any(Path(f).name == p.name for f in (t.get("files") or [])):
                return "log" in str((t.get("settings") or {}).get("picture_profile", "")).lower()
        return False

    def looks_list(_: Request):
        sh = handoff.current()
        files = []
        if sh:
            root = Path(sh["path"])
            for sub, kind in (("PHOTO JPEG", "photo"), ("VIDEO", "clip")):
                d = root / sub
                if d.is_dir():
                    files += [{"file": f"{sub}/{p.name}", "name": p.name, "kind": kind, "t": p.stat().st_mtime}
                              for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", *importer.VIDEO_EXT)]
        files.sort(key=lambda f: -f["t"])
        return JSONResponse({"looks": [{"key": lk["key"], "name": lk["name"], "for": lk["for"]} for lk in looks.LOOKS],
                             "files": files[:60], "job": {k: v for k, v in look_job.items() if k != "stop"},
                             "ffmpeg": video.find_ffmpeg() is not None})

    CARD_ART = ("portrait", "interview", "product", "daylight", "lowlight", "focusstack", "burst", "match", "auto")

    def card_art(request: Request):
        """The picture on a shot card or special-shot card: a stock photo shipped in camctl/assets/cards (Unsplash
        License, never the user). Only the named cards; the page keeps its drawing when there is none."""
        key = request.path_params["key"]
        f = Path(__file__).resolve().parent / "assets" / "cards" / f"{key}.jpg"
        if key not in CARD_ART or not f.is_file():
            return JSONResponse({"error": "no picture"}, status_code=404)
        return Response(f.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    _sample_cache: dict = {}

    def looks_sample(request: Request):
        """The six looks on the same sample picture: the stock photo shipped in camctl/assets (never the user)."""
        key = request.path_params["key"]
        if key != "original" and key not in looks.BY_KEY:
            return JSONResponse({"error": "unknown look"}, status_code=404)
        if key not in _sample_cache:
            base = looks.sample_picture()
            _sample_cache[key] = base if key == "original" else looks.apply_jpeg(base, key, 1.0, quality=90)
        return Response(_sample_cache[key], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    def looks_table(request: Request):
        key = request.path_params["key"]
        if key not in looks.BY_KEY:
            return JSONResponse({"error": "unknown look"}, status_code=404)
        return Response(looks.lut_bytes(key), media_type="application/octet-stream")

    def _still(p: Path) -> bytes:
        """A still of the file as the camera made it (a log clip is shown as a normal picture first)."""
        if p.suffix.lower() in (".jpg", ".jpeg"):
            from PIL import Image as _I
            im = _I.open(p).convert("RGB")
            im.thumbnail((960, 960))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=88)
            return b.getvalue()
        r = subprocess.run([video.ffmpeg(), "-hide_banner", "-v", "error", "-ss", "1", "-i", str(p), "-frames:v", "1",
                            "-vf", "scale=960:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                           capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        jpg = r.stdout
        if _clip_is_log(p) and jpg:
            jpg = lut.apply_table_to_jpeg(jpg, *lut.parse_cube(lut.export_cube(None, "log709", size=33, source="slog3cine")))
        return jpg

    async def looks_still(request: Request):
        """Before (key=original) / after still of one of his files, with the strength."""
        q = request.query_params
        try:
            p = _shoot_file(q.get("file", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        key = q.get("key", "original")
        try:
            strength = min(max(float(q.get("strength", "1")), 0.0), 1.0)
        except ValueError:
            strength = 1.0

        def work():
            base = _still(p)
            return base if key == "original" or key not in looks.BY_KEY else looks.apply_jpeg(base, key, strength, quality=88)
        try:
            return Response(await anyio.to_thread.run_sync(work), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Studio couldn't read that file."}, status_code=500)

    async def looks_copy(request: Request):
        """His press: a NEW file with the look under <shoot>\\LOOKS\\<look>\\ (photo now, clip in the background)."""
        body = await request.json()
        key = body.get("key")
        if key not in looks.BY_KEY:
            return JSONResponse({"error": "Choose a look first."}, status_code=400)
        try:
            p = _shoot_file(str(body.get("file", "")))
            strength = min(max(float(body.get("strength", 1.0)), 0.0), 1.0)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc) or "Choose a photo or clip from this shoot."}, status_code=400)
        shoot = Path(handoff.current()["path"])
        name = looks.BY_KEY[key]["name"]
        if p.suffix.lower() in (".jpg", ".jpeg"):
            try:
                out = await anyio.to_thread.run_sync(lambda: looks.copy_photo(p, shoot, key, strength))
            except OSError as exc:
                return JSONResponse({"error": "The disk is full, so the copy could not be saved." if getattr(exc, "errno", None) == 28
                                     else "The copy could not be saved in the shoot folder."}, status_code=500)
            autof._event(f"You made a copy of {p.name} with the look {name}", kind="you")
            return JSONResponse({"ok": True, "out": out.name, "say": f"Saved {out.name} in LOOKS › {name}. The original is unchanged."})
        if video.find_ffmpeg() is None:
            return JSONResponse({"error": "Studio's video tool (ffmpeg) is missing, so clips can't get a look. "
                                          "Run \"Install or repair camctl Studio\" once."}, status_code=400)
        if look_job["busy"]:
            return JSONResponse({"error": "A copy is already being made. Wait for it, or press Stop."}, status_code=409)
        look_job.update(busy=True, out=None, message=f"Making a copy of {p.name} with {name}…")
        look_job["stop"].clear()

        def work():
            try:
                out = looks.copy_clip(p, shoot, key, strength, log=_clip_is_log(p), stop=look_job["stop"])
                look_job.update(out=out.name, message=f"Saved {out.name} in LOOKS › {name}. The original is unchanged.")
                autof._event(f"You made a copy of {p.name} with the look {name}", kind="you")
            except InterruptedError:
                look_job.update(message="Stopped. Nothing was saved and the original is unchanged.")
            except Exception:  # noqa: BLE001
                look_job.update(message="The copy could not be made. The original is unchanged.")
            finally:
                look_job["busy"] = False
        threading.Thread(target=work, daemon=True, name="look-copy").start()
        return JSONResponse({"ok": True, "busy": True, "say": look_job["message"]})

    def looks_stop(_: Request):
        look_job["stop"].set()
        return JSONResponse({"ok": True})

    ui_ctx: dict = {"panel": None, "look": None}          # what the page has open (no content, only names)

    def _active_tools() -> list[dict]:
        """The tools in use right now, each {key, detail} (assistant.TOOL_RULES gives Claude the priorities)."""
        out = []
        if (series_state.get("kind") == "focusstack" and auto.status.get("running")) or ui_ctx.get("panel") == "focusstack":
            out.append({"key": "focusstack", "detail": "a focus stack series" + (" is running" if auto.status.get("running") else " is being set up")})
        if play["long_on"]:
            out.append({"key": "long", "detail": f"long exposure, {play['long_s']} s applied" if play["long_before"] is not None
                        else "long exposure, setup not applied yet"})
        if play["double_on"]:
            out.append({"key": "double", "detail": "double exposure, composing photo 2 over the ghost of photo 1"
                        if play["first"] and not play["second"] else "double exposure, photo 1 of 2"})
        if play["pull"].get("a") or play["pull"].get("b") or ui_ctx.get("panel") == "pull":
            p = play["pull"]
            out.append({"key": "pull", "detail": "focus pull" + (f" from position {p['a']['pos']} to {p['b']['pos']}"
                                                                  if p.get("a") and p.get("b") else ", points not set yet")})
        if studio.reel.on:
            out.append({"key": "reel", "detail": "day reel on (the 3 s before each photo are kept)"})
        if ui_ctx.get("panel") in ("sound", "teleprompter", "shotlist"):
            out.append({"key": ui_ctx["panel"], "detail": {"sound": "the sound check result is open",
                                                           "teleprompter": "the teleprompter is open",
                                                           "shotlist": "the shot list is open"}[ui_ctx["panel"]]})
        return out

    async def claude_context(request: Request):
        """The page says which tool panel is open and whether a look preview is on (names only, never pictures)."""
        b = await request.json()
        if "panel" in b:
            ui_ctx["panel"] = b["panel"] if b["panel"] in ("focusstack", "pull", "sound", "teleprompter", "shotlist") else None
        if "look" in b:
            ui_ctx["look"] = str(b["look"])[:40] if b["look"] else None
        return JSONResponse({"tools": [t["key"] for t in _active_tools()], "look": ui_ctx["look"]})

    def _situation() -> dict:
        """The situation every camera button sends Claude (assistant.SITUATION): mode, format, shot, locks, offers."""
        props = studio._props or {}
        em = props.get(PROPS["exposure_mode"])
        mode = "video" if em is not None and em.value in autofix.MOVIE_MODES else "photo"
        prof = camprofile.of(studio)
        fs = formats.summary(mode, props, prof, label) if props else {}
        s = {"mode": mode, "format": fs.get("name"), "pp_kind": fs.get("pp_kind"),
             "aspect": fs.get("ratio_name") if mode == "photo" else None,
             "shot": (autof.decision or {}).get("shot_name")}
        if mode == "video" and props.get(SP_MOVIE_SETTING):
            fps = capture.fps_of(label("movie_setting", props[SP_MOVIE_SETTING].value))
            t = capture.video_shutter(fps, int(prefs().get("mains_hz", 50)))[0]
            s["shutter_rule"] = f"{fps}p with {int(prefs().get('mains_hz', 50))} Hz mains: 1/{round(1 / t)} s, never changed"
        locked = [n for n in ("aperture", "shutter_speed", "iso", "white_balance", "focus_mode") if n in (autof.paused or {})]
        if focus_state.get("text"):
            locked.append("focus " + str(focus_state["text"]))
        s["locked"] = ", ".join(locked) if locked else "Studio's setup"
        vals = {x["name"]: x for x in settings_view(props)} if props else {}
        offers = []
        for n, t in (("aperture", "aperture"), ("iso", "ISO")):
            ch = [c["label"] for c in vals.get(n, {}).get("choices", []) if c["value"] not in (0, 0xFFFFFF)]
            if ch:
                offers.append(f"{t} {ch[0]} to {ch[-1]}")
        s["offered"] = "; ".join(offers)
        s["tools"] = _active_tools()
        if ui_ctx.get("look"):
            s["look_preview"] = ui_ctx["look"]
        if mode == "video":
            sh = handoff.current()
            vt = [t for t in (importer.takes(sh) if sh else []) if t.get("kind") == "video" and t.get("settings")]
            if vt:
                st = vt[-1]["settings"]
                s["last_take"] = ", ".join(f"{k.replace('_', ' ')} {st[k]}" for k in
                                           ("aperture", "shutter_speed", "iso", "white_balance", "color_temp") if st.get(k))
        return s
    assistant.SITUATION = _situation

    # -- CAPTURE CARD (optional, not yet tested on a real card): picture source and recording on the laptop --------
    card_rec: dict = {"state": "idle", "t0": 0.0, "say": "", "file": None, "mkv": None}

    def _card_state() -> dict:
        devs = card_state.get("devices") or {"video": [], "audio": [], "ffmpeg": video.find_ffmpeg() is not None}
        c = getattr(studio, "card", None)
        lvl = c.level() if c is not None else None
        return {"source": "card" if c is not None else "camera", "devices": devs,
                "chosen": card_state.get("chosen"), "size": list(c.size) if c is not None and c.size else None,
                "level_db": None if lvl is None else round(lvl, 1), "note": getattr(studio, "card_note", ""),
                "record_to": "laptop" if prefs().get("record_to") == "laptop" else "card",
                "rec": {k: card_rec[k] for k in ("state", "say")} | {"elapsed": round(time.time() - card_rec["t0"], 1)
                                                                    if card_rec["state"] == "recording" else 0}}

    card_state: dict = {"devices": None, "chosen": None}

    async def card_view(request: Request):
        """GET: the state (device names only after he opened this page; nothing is opened by listing).
        POST {list} / {source:'camera'} / {source:'card', video, audio} / {record_to}."""
        if request.method == "POST":
            b = await request.json()
            if b.get("list"):
                card_state["devices"] = await anyio.to_thread.run_sync(capture_card.list_devices)
            if "record_to" in b:
                save_prefs(record_to="laptop" if b["record_to"] == "laptop" else "card")
            if b.get("source") == "camera":
                if card_rec["state"] in ("starting", "recording", "finishing"):
                    return JSONResponse({"error": "Stop the recording on the laptop first."}, status_code=409)
                studio.card_fallback("")
                card_state["chosen"] = None
                autof._event("You switched the picture back to the camera cable", kind="you")
            elif b.get("source") == "card":
                devs = card_state.get("devices") or await anyio.to_thread.run_sync(capture_card.list_devices)
                card_state["devices"] = devs
                if not devs.get("ffmpeg"):
                    return JSONResponse({"error": "The capture card needs Studio's video tool (ffmpeg). Run \"Install "
                                                  "or repair camctl Studio\" once."}, status_code=400)
                v, a = b.get("video"), b.get("audio")
                if v not in devs["video"] or (a and a not in devs["audio"]):
                    return JSONResponse({"error": "Choose the capture card from the list first."}, status_code=400)
                old = getattr(studio, "card", None)
                if old is not None:
                    studio.card_fallback("")
                src = capture_card.CardSource(v, a or None)
                try:
                    await anyio.to_thread.run_sync(src.start)
                except Exception:  # noqa: BLE001
                    return JSONResponse({"error": "The capture card could not be opened. Check that it is plugged in."},
                                        status_code=400)
                studio.card_note = ""
                studio._card_seq = 0
                studio.card = src
                card_state["chosen"] = {"video": v, "audio": a}
                autof._event("You chose the capture card as the picture", kind="you")   # never the device name
        return JSONResponse(_card_state())

    def card_levels(request: Request):
        """The card's sound levels (dBFS per 50 ms) since a time: the Sound check can listen through the card."""
        c = getattr(studio, "card", None)
        try:
            t0 = float(request.query_params.get("since", "0"))
        except ValueError:
            t0 = 0.0
        return JSONResponse({"levels": c.levels_since(t0) if c is not None else [], "now": time.time()})

    def _card_record(action: str) -> dict:
        """His press on record while "Record video to: This laptop" and the card is the source."""
        c = getattr(studio, "card", None)
        if c is None:
            raise ValueError("Choose the capture card as the picture first, or record to the camera's card.")
        sh = Path(handoff.ensure_shoot()["path"])
        if action == "start":
            if card_rec["state"] in ("starting", "recording", "finishing"):
                return {"ok": True, **card_rec}
            if capture_card.disk_free(sh) < capture_card.MIN_FREE_START:
                raise ValueError("The laptop's disk is almost full (less than 2 GB free), so the recording was not "
                                 "started. Free some space first.")
            mkv = sh / "VIDEO" / (time.strftime("%Y%m%d-%H%M%S") + " laptop.mkv")
            card_rec.update(state="starting", say="Starting…", mkv=mkv, file=None, t0=0.0)
            studio.card_recording = True
            c.start(record_path=mkv)                     # about a second without picture: one process owns the device

            def watch():
                end = time.time() + 10
                while time.time() < end and card_rec["state"] == "starting":
                    if c.writing():
                        card_rec.update(state="recording", t0=time.time(), say="Recording on the laptop")
                        studio.recording, studio.rec_since = True, time.time()
                        break
                    time.sleep(0.2)
                if card_rec["state"] == "starting":
                    c.start()                            # back to the picture only
                    studio.card_recording = False
                    card_rec.update(state="idle", say="The recording on the laptop did not start. Nothing was saved.")
                    return
                while card_rec["state"] == "recording":     # disk guard while recording
                    if capture_card.disk_free(sh) < capture_card.MIN_FREE_KEEP:
                        _card_stop("The laptop's disk is almost full, so Studio stopped the recording. ")
                        return
                    time.sleep(2)
            threading.Thread(target=watch, daemon=True, name="card-rec").start()
            return {"ok": True, **{k: card_rec[k] for k in ("state", "say")}}
        _card_stop("")
        return {"ok": True, **{k: card_rec[k] for k in ("state", "say")}}

    def _card_stop(prefix: str) -> None:
        c = getattr(studio, "card", None)
        if card_rec["state"] not in ("starting", "recording"):
            return
        card_rec.update(state="finishing", say="Finishing the file…")
        studio.recording = False
        start_t = card_rec["t0"] or time.time()

        def work():
            if c is not None:
                c.start()                                # stop writing, keep the picture
            studio.card_recording = False
            mkv = card_rec["mkv"]
            out, say = capture_card.finish_file(mkv) if mkv and mkv.exists() else (None, "Nothing was recorded.")
            card_rec.update(state="saved" if out else "failed", say=prefix + say, file=str(out) if out else str(mkv))
            if out:
                try:
                    importer.log_take("video", start_t, time.time(), files=[str(out)])
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=work, daemon=True, name="card-finish").start()

    # -- PLAYGROUND EXPERIMENTS: double exposure, long exposure (+ practice), shoot for a mood, focus pull ----------
    play: dict = {"double_on": False, "first": None, "second": None, "result": None, "blend": "bright",
                  "long_on": False, "long_s": None, "long_before": None, "long_entry": None, "long_phase": None,
                  "long_open_until": 0.0, "mood": None, "mood_words": "",
                  "pull": {"a": None, "b": None, "busy": False, "say": "", "pick": None}}

    def _shoot() -> Path:
        sh = handoff.current() or handoff.ensure_shoot()
        return Path(sh["path"])

    def _jpeg_of(paths: list[str]) -> Path | None:
        return next((Path(p) for p in paths if str(p).lower().endswith((".jpg", ".jpeg")) and Path(p).exists()), None)

    def _double_after_photo(paths: list[str]) -> dict | None:
        """His photo 1 or 2 of a double exposure. Photo 1 stays a normal photo; after photo 2 the blend is saved."""
        if not play["double_on"]:
            return None
        jpg = _jpeg_of(paths)
        if jpg is None:
            return {"say": "The double exposure needs the camera's JPEG. Choose RAW + JPEG or JPEG in Format."}
        if play["first"] is None:
            play.update(first=str(jpg), second=None, result=None)
            return {"step": 2, "say": "Photo 1 saved. Its picture now lies over the live view: compose photo 2."}
        play["second"] = str(jpg)
        out = playground.double_save(Path(play["first"]), jpg, _shoot(), play["blend"])
        play["result"] = str(out)
        autof._event(f"Double exposure saved: {out.name}", kind="you")
        return {"step": 3, "say": f"Saved {out.name} in PLAYGROUND › Double exposure. Both photos are kept as they are.",
                "file": out.name}

    def _play_state() -> dict:
        p = play["pull"]
        sh = studio._props or {}                 # cached only: never wait for the camera while the shutter is open
        sp = sh.get(PROPS["shutter_speed"]) if sh else None
        offered = playground.long_offered(list(sp.choices or []) if sp else [], presets.shutter_seconds)
        now = time.time()
        phase = play["long_phase"]
        left = max(0, int(math.ceil(play["long_open_until"] - now))) if phase == "open" else 0
        if phase == "open" and left == 0:
            phase = "finishing"
        return {"double": {"on": play["double_on"], "step": 2 if play["first"] and not play["second"] else 1,
                           "first": bool(play["first"]), "result": Path(play["result"]).name if play["result"] else None,
                           "blend": play["blend"]},
                "long": {"on": play["long_on"], "offered": sorted(offered), "seconds": play["long_s"],
                         "applied": play["long_before"] is not None, "phase": phase, "left": left,
                         "mode_ok": studio.cam is None or not (sh and sh.get(PROPS["exposure_mode"]) and
                                                               sh[PROPS["exposure_mode"]].value in autofix.MOVIE_MODES)},
                "mood": {"words": play["mood_words"], "proposal": play["mood"]},
                "pull": {k: p[k] for k in ("a", "b", "busy", "say", "pick")}}

    async def play_view(request: Request):
        """GET the experiments' state; POST {double:on/off/reset/blend, long:on/off, pull_pick:'a'|'b'|null}."""
        if request.method == "POST":
            b = await request.json()
            if "double" in b:
                v = b["double"]
                if v in ("on", "off", "reset"):
                    play.update(first=None, second=None, result=None)
                    play["double_on"] = v != "off" if v != "reset" else play["double_on"]
                elif v in playground.BLENDS and play["first"] and play["second"]:
                    play["blend"] = v
                    old = Path(play["result"]) if play["result"] else None
                    out = await anyio.to_thread.run_sync(
                        lambda: playground.double_save(Path(play["first"]), Path(play["second"]), _shoot(), v))
                    if old and old != out and old.exists():
                        old.unlink()                     # his choice of blend replaces the one Studio made, never a photo
                    play["result"] = str(out)
            if "long" in b:
                play["long_on"] = bool(b["long"])
            if "pull_pick" in b:
                play["pull"]["pick"] = b["pull_pick"] if b["pull_pick"] in ("a", "b") else None
        return JSONResponse(_play_state())

    def play_image(request: Request):
        which = request.path_params["which"]
        p = {"first": play["first"], "second": play["second"], "result": play["result"]}.get(which)
        if not p or not Path(p).exists():
            return JSONResponse({"error": "nothing yet"}, status_code=404)
        return Response(playground.preview(Path(p), 1280 if which == "first" else 960), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    async def long_apply(request: Request):
        """His Apply of the long-exposure setup (logged with undo); the long shutter is his choice from here."""
        b = await request.json()
        if studio.recording:
            return JSONResponse({"error": "Stop the recording first."}, status_code=409)
        secs = int(b.get("seconds") or 0)
        props = studio.props(max_age=0)
        em = props.get(PROPS["exposure_mode"])
        if em is not None and em.value in autofix.MOVIE_MODES:
            return JSONResponse({"error": "Switch to Photo first: a long exposure is a photo."}, status_code=400)
        codes = {n: code_of(n) for n in ("shutter_speed", "iso", "aperture", "focus_mode", "white_balance", "color_temp")}
        ch = playground.long_setup(props, secs, codes, presets.shutter_seconds)
        if not ch:
            return JSONResponse({"error": "This camera does not offer long shutter times over the cable."}, status_code=400)
        before = {s["name"]: s["value"] for s in settings_view(props) if s["name"] in {c["name"] for c in ch}}

        def work():
            autof.studio_busy(20)
            r = changelog.apply(ch, "you", f"Long exposure: {secs} s")
            for c in ch:
                autof.manual(c["name"])                  # his choice: no "too slow", no compensation
            return r
        r = await anyio.to_thread.run_sync(work)
        play.update(long_before=before if play["long_before"] is None else play["long_before"], long_s=secs)
        claude_intent.clear()
        return JSONResponse({**_play_state(), "applied": r.get("applied"),
                             "say": f"Locked for a long exposure: {secs} s. Press the shutter, then draw with light."})

    async def long_restore(_: Request):
        """"Back to Studio's choice": the setup he had before the long exposure."""
        before = play["long_before"]
        if not before:
            return JSONResponse(_play_state())
        order = ["white_balance", "color_temp", "focus_mode", "aperture", "iso", "shutter_speed"]
        ch = [{"name": n, "value": before[n]} for n in order if n in before]
        await anyio.to_thread.run_sync(lambda: (autof.studio_busy(20), changelog.apply(ch, "you", "Back from the long exposure")))
        play.update(long_before=None, long_s=None)
        return JSONResponse({**_play_state(), "say": "Back to the setup you had before."})

    async def paint_save(request: Request):
        """The Practice picture (made in the page from the live picture) into PLAYGROUND › Paint with light."""
        raw = await request.body()
        if len(raw) > 8_000_000 or raw[:2] != b"\xff\xd8":
            return JSONResponse({"error": "That isn't a practice picture."}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(lambda: playground.save_practice(raw, _shoot()))
        except OSError:
            return JSONResponse({"error": "The picture could not be saved in the shoot folder."}, status_code=500)
        return JSONResponse({"ok": True, "say": f"Saved {out.name} in PLAYGROUND › Paint with light."})

    def _nearest(choices, want, key=lambda v: v):
        return min(choices, key=lambda v: abs(math.log(max(key(v), 1e-6)) - math.log(max(want, 1e-6))))

    async def mood_ask(request: Request):
        """Shoot for a mood: ONE Claude call per press, on the untouched frame; values clamped to what the camera
        offers (and said so). Nothing changes until his Apply."""
        b = await request.json()
        words = str(b.get("words", "")).strip()[:300]
        if not words:
            return JSONResponse({"error": "Say the mood in a few words first."}, status_code=400)
        if not assistant.has_key():
            return JSONResponse({"error": "NO_KEY"}, status_code=400)
        props = studio.props(max_age=0)
        vals = {s["name"]: s for s in settings_view(props)}
        em = props.get(PROPS["exposure_mode"])
        video_mode = em is not None and em.value in autofix.MOVIE_MODES
        aps = sorted(c["value"] for c in vals.get("aperture", {}).get("choices", []) if c["value"])
        shs = sorted((c["value"] for c in vals.get("shutter_speed", {}).get("choices", []) if presets.shutter_seconds(c["value"])),
                     key=presets.shutter_seconds)
        isos = sorted(c["value"] for c in vals.get("iso", {}).get("choices", []) if 0 < c["value"] < 0xFFFFFF)
        kel = props.get(SP_COLOR_TEMP)
        krange = list(kel.range[:2]) if kel is not None and kel.range else [2500, 9900]
        fixed = None
        if video_mode:
            ms = props.get(SP_MOVIE_SETTING)
            fps = capture.fps_of(label("movie_setting", ms.value)) if ms else 25
            fixed = capture.video_shutter(fps, int(prefs().get("mains_hz", 50)))[0]
        if not (aps and shs and isos):
            return JSONResponse({"error": "The camera isn't reporting its values right now. Try again in a moment."}, status_code=400)
        ranges = {"mode": "video" if video_mode else "photo", "aperture": [aps[0] / 100, aps[-1] / 100],
                  "shutter_s": [presets.shutter_seconds(shs[0]), presets.shutter_seconds(shs[-1])],
                  "iso": [isos[0], isos[-1]], "kelvin": krange, "fixed_shutter_s": fixed}
        frame = studio.frame or live_frame()

        def work():
            return assistant.mood(frame, words, ranges)
        try:
            ans = await anyio.to_thread.run_sync(work)
        except assistant.AskError as exc:
            return _claude_err(exc)
        clamped, changes = [], []
        ap = _nearest(aps, float(ans["aperture_f"]) * 100)
        sh = _nearest(shs, float(fixed or ans["shutter_s"]), presets.shutter_seconds)
        iso = _nearest(isos, float(ans["iso"]))
        kv = int(min(max(int(ans["kelvin"]), krange[0]), krange[1]))
        step = (kel.range[2] if kel is not None and kel.range and kel.range[2] else 100)
        kv = int(round(kv / step) * step)
        if abs(ap / 100 - float(ans["aperture_f"])) > 0.35:
            clamped.append(f"aperture f/{float(ans['aperture_f']):g} → {label('aperture', ap)}")
        if not fixed and abs(math.log2(presets.shutter_seconds(sh) / max(float(ans["shutter_s"]), 1e-6))) > 0.4:
            clamped.append(f"shutter {float(ans['shutter_s']):g} s → {label('shutter_speed', sh)}")
        if abs(math.log2(iso / max(float(ans["iso"]), 1))) > 0.4:
            clamped.append(f"ISO {ans['iso']} → {iso}")
        if kv != int(ans["kelvin"]) and abs(kv - int(ans["kelvin"])) > step:
            clamped.append(f"{ans['kelvin']} K → {kv} K")
        for name, v in (("aperture", ap), ("shutter_speed", sh), ("iso", iso)):
            if vals.get(name) and vals[name]["value"] != v:
                changes.append({"name": name, "value": v, "label": label(name, v), "from_label": vals[name]["label"]})
        if vals.get("white_balance") and 32786 in [c["value"] for c in vals["white_balance"]["choices"]]:
            if vals["white_balance"]["value"] != 32786:
                changes.append({"name": "white_balance", "value": 32786, "label": "Color temp", "from_label": vals["white_balance"]["label"]})
            if vals.get("color_temp", {}).get("value") != kv:
                changes.append({"name": "color_temp", "value": kv, "label": f"{kv} K", "from_label": vals.get("color_temp", {}).get("label", "")})
        prop = {"words": words, "changes": changes, "guide": ans["guide"], "light": ans["light"], "room": ans["room"],
                "cannot": ans["cannot"], "clamped": clamped,
                "note": ("Some values were outside what the camera offers, so Studio used the nearest: " + "; ".join(clamped) + ".")
                if clamped else "", "shutter_rule": bool(fixed)}
        play.update(mood=prop, mood_words=words,
                    mood_before={c["name"]: vals[c["name"]]["value"] for c in changes if c["name"] in vals})
        return JSONResponse(prop)

    async def mood_back(_: Request):
        """"Back to Studio's choice" after a mood: the values he had before he applied it."""
        before = play.get("mood_before") or {}
        order = ["white_balance", "color_temp", "aperture", "iso", "shutter_speed"]
        ch = [{"name": n, "value": before[n]} for n in order if n in before]
        if ch:
            await anyio.to_thread.run_sync(lambda: (autof.studio_busy(20), changelog.apply(ch, "you", "Back from the mood")))
        claude_intent.clear()
        play.update(mood=None, mood_before=None)
        return JSONResponse({**_play_state(), "say": "Back to the setup you had before the mood."})

    async def pull_mark(request: Request):
        """His tap for point A or B: focus there once, lock MF, remember the lens position."""
        b = await request.json()
        which = b.get("which")
        if which not in ("a", "b"):
            return JSONResponse({"error": "Tap A or B."}, status_code=400)
        x, y = float(b.get("x", .5)), float(b.get("y", .5))

        def work():
            studio.run(lambda c: c.focus_at(x, y))
            time.sleep(0.6)
            studio.run(lambda c: c.set("focus_mode", 1) if 1 in c.refresh()[PROPS["focus_mode"]].choices else None)
            return focus_pos_now()
        pos = await anyio.to_thread.run_sync(work)
        if pos is None:
            return JSONResponse({"error": "The camera doesn't report a focus position, so a pull can't be planned."}, status_code=400)
        play["pull"][which] = {"pos": pos, "x": x, "y": y}
        play["pull"]["pick"] = None
        return JSONResponse(_play_state())

    async def pull_run(request: Request):
        """His press: move the focus between A and B in small even steps. Never starts or stops a recording."""
        b = await request.json()
        p = play["pull"]
        if not (p["a"] and p["b"]):
            return JSONResponse({"error": "Tap point A and point B first."}, status_code=400)
        if p["busy"]:
            return JSONResponse(_play_state())
        start, end = (p["a"], p["b"]) if b.get("direction", "ab") == "ab" else (p["b"], p["a"])
        secs = float(b.get("seconds", 2))
        secs = secs if secs in (1.0, 2.0, 4.0) else 2.0
        fm = studio.props(max_age=0).get(PROPS["focus_mode"])
        if fm is None or fm.value not in (1, 32774):
            studio.run(lambda c: c.set("focus_mode", 1))
        p.update(busy=True, say="Pulling focus…")

        def work():
            try:
                focus_goto_pos(int(start["pos"]))
                now = focus_pos_now() or start["pos"]
                steps, gap = playground.pull_plan(int(now), int(end["pos"]), secs)
                for s in steps:
                    studio.run(lambda c, s=s: c.focus_step(s))
                    time.sleep(gap)
                ended = focus_pos_now()
                p["say"] = f"Focus stays where the pull ended ({'B' if end is p['b'] else 'A'}, position {ended})."
            except Exception:  # noqa: BLE001
                p["say"] = "The pull stopped part-way. Focus stays where it is."
            finally:
                p["busy"] = False
        threading.Thread(target=work, daemon=True, name="focus-pull").start()
        return JSONResponse(_play_state())

    SOUND_FIELDS = ("noise_dbfs", "hum_db", "voice_dbfs", "clipped", "echo_s", "mains_hz")

    async def sound_ask(request: Request):
        """His press on the Claude spark under a sound check: only the NUMBERS go into one question (the sound was
        never recorded; anything else in the request is ignored)."""
        raw = await request.body()
        if len(raw) > 2000:
            return JSONResponse({"error": "Only the sound check's numbers can be sent."}, status_code=400)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        nums = {}
        for k in SOUND_FIELDS:
            v = body.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                nums[k] = round(float(v), 1)
        if not assistant.has_key():
            return JSONResponse({"error": "NO_KEY"}, status_code=400)
        text = ("A sound check before recording a talking-head video, measured through a laptop microphone (not the "
                "camera's microphone). Numbers: " + json.dumps(nums) + ". noise_dbfs is the quiet room, voice_dbfs the "
                "90th percentile of speech, hum_db how far a mains hum stands above its neighbours (null = none), "
                "echo_s an estimated reverberation time (null = not clear). In two or three plain sentences, like a "
                "sound person on set, say what matters most and what to do. No jargon without a plain word beside it.")

        def work():
            with assistant.feature("sound check"):
                data = assistant.call({"model": assistant.MODEL, "max_tokens": 16000,
                                       "messages": [{"role": "user", "content": text}]}, assistant._key())
            if data.get("stop_reason") == "refusal":
                raise assistant.AskError("Claude declined this question. The numbers above still say what to fix.")
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        try:
            return JSONResponse({"say": await anyio.to_thread.run_sync(work), "sent": nums})
        except assistant.AskError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def _format_rows() -> dict:
        _migrate_photo_log()
        props = studio.props(max_age=0)
        prof = camprofile.of(studio)
        em = props.get(PROPS["exposure_mode"])
        active = "video" if em is not None and em.value in autofix.MOVIE_MODES else "photo"
        formats.remember(active, props)
        pl = bool(prefs().get("photo_log", False))
        return {"active": active, "summary": formats.summary(active, props, prof, label),
                "photo": formats.rows("photo", props, prof, label, active == "photo", pl),
                "video": formats.rows("video", props, prof, label, active == "video", pl)}

    BASIS = {"picture_profile", "movie_format", "movie_setting", "still_format"}     # these change how to expose

    async def formats_view(request: Request):
        """All settings › Camera › Format. GET: both modes' rows (what the camera offers, his choice, Studio's
        choice). POST {mode, key, value}: his pick (value null = back to Studio's choice). Only on his press; a value
        the camera refuses keeps the previous one and says so calmly; a new exposure basis gets one fresh setup."""
        if request.method == "GET":
            try:
                return JSONResponse(await anyio.to_thread.run_sync(_format_rows))
            except Exception as exc:  # noqa: BLE001
                return err(exc)
        body = await request.json()
        mode, key = body.get("mode"), body.get("key")
        if mode not in formats.ROWS or key not in formats.ROWS[mode]:
            return JSONResponse({"error": "That is not a format setting."}, status_code=400)
        value = body.get("value")
        try:
            value = None if value is None else int(value)
        except (TypeError, ValueError):
            return JSONResponse({"error": "That is not a value the camera offers."}, status_code=400)
        if studio.recording:
            return JSONResponse({"error": "Stop the recording to change the format."}, status_code=409)
        if dirx.status["running"]:
            return JSONResponse({"error": "Stop the director first."}, status_code=400)

        def work():
            props = studio.props(max_age=0)
            prof = camprofile.of(studio)
            em = props.get(PROPS["exposure_mode"])
            active = "video" if em is not None and em.value in autofix.MOVIE_MODES else "photo"
            title = formats.TITLES[(mode, key)]
            code = formats.CODES[key]
            p = props.get(code)
            before = p.value if p is not None else None
            target = value
            if value is None:
                formats.set_choice(mode, key, None)
                say = f"{title}: back to Studio's choice ({formats.default_name(mode, key, prof)})."
                if mode == active:
                    target = formats.studio_value(mode, key, props, prof, label, bool(prefs().get("photo_log", False)))
            else:
                name = formats.value_name(key, value, prof, label)
                offered = [c["value"] for r in formats.rows(mode, props, prof, label, mode == active) if r["key"] == key
                           for c in r["choices"]]
                if value not in offered:
                    return {"ok": False, "say": f"The camera doesn't offer {name} here, so {title.lower()} stays as it is."}
                if mode != active:
                    formats.set_choice(mode, key, value)
                    autof._event(f"You chose {title.lower()}: {name} (used when you switch to {mode})", kind="you")
                    return {"ok": True, "say": f"{title}: {name}. Studio uses it when you switch to {mode.title()}."}
                say = f"{title}: {name}."
            changed = False
            if mode == active and target is not None and target != before:
                autof.studio_busy(30)
                try:
                    studio.run(lambda c: c.set(code, target))
                    time.sleep(0.4)
                    now = studio.props(max_age=0).get(code)
                    ok = now is not None and now.value == target
                except Exception:  # noqa: BLE001 - the camera said no: nothing stored, the previous value stays
                    ok = False
                if not ok:
                    return {"ok": False, "say": f"The camera didn't take {formats.value_name(key, target, prof, label)} "
                                                f"right now, so it stays on {formats.value_name(key, before, prof, label)}. "
                                                "Some formats need another camera mode or menu setting first."}
                changed = True
            if value is not None:
                formats.set_choice(mode, key, value)
            if not (changed and key in BASIS):
                autof._event(("You chose " + say[0].lower() + say[1:]).rstrip("."), kind="you")
            if changed and key in BASIS:
                # like the Photo/Video switch: built-in rules only (no Claude), logged with undo, Locked afterwards
                fname = formats.summary(mode, studio.props(max_age=0), prof, label)["name"] or mode
                mode_setup(mode, who="You", event=f"You chose {title.lower()}: set up again for {fname}")
                setup = f"Set up again for {formats.summary(mode, studio.props(max_age=0), prof, label)['name'] or mode}."
                return {"ok": True, "say": say + " " + setup, "setup": setup}
            return {"ok": True, "say": say}
        try:
            out = await anyio.to_thread.run_sync(work)
            return JSONResponse({**out, **(await anyio.to_thread.run_sync(_format_rows))})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def settings(_: Request):
        try:
            props = studio.props()
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        prof = camprofile.of(studio)
        ppv = props.get(presets.PROP_PICTURE_PROFILE)
        kind = camprofile.log_kind(prof, ppv.value if ppv else None)
        em = props.get(PROPS["exposure_mode"])
        fmode = "video" if em is not None and em.value in autofix.MOVIE_MODES else "photo"
        try:
            fsum = formats.summary(fmode, props, prof, label)
            formats.remember(fmode, props)
        except Exception:  # noqa: BLE001
            fsum = {"mode": fmode, "name": "", "ratio": None, "pp_kind": "standard", "jpeg_only": False}
        return JSONResponse({"camera": studio.cam.name if studio.cam else "",
                             "format": fsum,
                             "profile": {"model": prof.get("model"), "tested": bool(prof.get("tested")),
                                         "log_confirmed": bool(prof.get("log_confirmed")), "log_pp": prof.get("log_pp"),
                                         "log_min_iso": prof.get("log_min_iso"), "zoom_mm": bool((prof.get("lens") or {}).get("zoom_mm"))},
                             "log": {"active": kind is not None, "kind": kind, "note": getattr(studio, "pp_note", "")},
                             "settings": settings_view(props),
                             **status_view(props),
                             "recording": studio.recording,     # Studio's own intent, never the camera's flaky flag
                             "assistant_ready": assistant.has_key()})

    NICE = {"exposure_mode": "The mode", "aperture": "The aperture", "shutter_speed": "The shutter speed", "iso": "ISO",
            "exposure_compensation": "Exposure compensation", "white_balance": "White balance",
            "color_temp": "The colour temperature", "focus_mode": "The focus mode", "focus_area": "The focus area",
            "picture_profile": "The picture profile"}

    def why_not(name: str, info, value: int) -> str | None:
        """The reason in plain words when the camera cannot take this value right now (None = it can)."""
        nice = NICE.get(name, name.replace("_", " ").capitalize())
        if info is None:
            return f"{nice} is not offered by this camera right now."
        if not (info.writable and info.enabled):
            return f"{nice} cannot be changed on the camera in this mode."
        ok = [v for v in (info.settable or []) if v not in (0, 0xFFFF, 0xFFFFFF, 0xFFFFFFFF)]
        if info.form == "enum" and ok and value not in info.settable:
            if name == "aperture" and value < min(ok):
                return f"The lens cannot open wider than {label(name, min(ok))} at this zoom."
            if name == "aperture" and value > max(ok):
                return f"The lens cannot close further than {label(name, max(ok))}."
            if name == "iso" and value < min(ok):
                prof = camprofile.of(studio)
                pp = (studio.props(max_age=5).get(code_of("picture_profile")) or None) if "picture_profile" in CONTROLS else None
                low = f"The lowest ISO the camera allows right now is {label(name, min(ok))}"
                if pp is not None and camprofile.is_log(prof, pp.value):
                    return low + " (S-Log keeps ISO up there)."
                return low + "."
            return f"{label(name, value)} is not available on the camera right now."
        if info.form == "range" and info.range and not (info.range[0] <= value <= info.range[1]):
            return f"{nice} goes from {label(name, info.range[0])} to {label(name, info.range[1])}."
        return None

    async def set_setting(request: Request):
        """His tap on a value. Checked against what the camera allows right now, written, read back; when the
        camera does not take it, the answer says why in plain words."""
        body = await request.json()
        name, value = body["name"], int(body["value"])
        if name not in CONTROLS:
            return JSONResponse({"error": "unknown setting"}, status_code=400)
        if zoom_state.get("busy") or zoom_state.get("moving"):
            return JSONResponse({"error": "The lens is zooming: tap again when it stops."}, status_code=409)
        autof.manual(name)
        code = code_of(name)

        def apply(c):
            info = c.refresh().get(code)
            why = why_not(name, info, value)
            if why:
                return why, info.value if info is not None else None
            try:
                c.set(code, value)
            except Exception as exc:  # noqa: BLE001  the camera said no (the real one: "failed with response 0x...")
                if "refused" not in str(exc) and "failed with response" not in str(exc):
                    raise
                cur = c.refresh().get(code)
                if name == "aperture" and cur is not None and value < cur.value:
                    return f"The lens cannot open wider than {label(name, cur.value)} at this zoom.", cur.value
                return f"{label(name, value)} is not available on the camera right now.", cur.value if cur else None
            cur = c.refresh().get(code)
            end = time.time() + (6.0 if name == "exposure_mode" else 2.0)   # a mode change settles slowly (12 Nov 2026)
            while cur is not None and cur.value != value and time.time() < end:
                time.sleep(0.25)
                cur = c.refresh().get(code)
            if cur is None or cur.value == value:
                return None, value
            if name == "aperture" and cur.value > value:
                return f"The lens cannot open wider than {label(name, cur.value)} at this zoom.", cur.value
            return f"The camera kept {label(name, cur.value)} instead of {label(name, value)}.", cur.value

        try:
            why, now = studio.run(apply)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": "The camera did not answer in time. Tap again.", "detail": str(exc)[:300]}, status_code=502)
        if why:
            return JSONResponse({"error": why, "value": now}, status_code=409)
        return JSONResponse({"ok": True, "value": now})

    focus_job: dict = {"gen": 0, "lock_ms": None}

    async def focus_point(request: Request):
        """Tap to focus: acquire once at the point (AF-S, or AF-C in video), verify by sharpness, lock in MF.
        Says in words when focus could not be found instead of showing a lock."""
        body = await request.json()
        xf, yf = min(max(float(body.get("x", 0.5)), 0.0), 1.0), min(max(float(body.get("y", 0.5)), 0.0), 1.0)
        autof.manual("focus_mode")

        def sharp_at(frame):
            from PIL import Image as _I
            im = _I.open(__import__("io").BytesIO(frame)).convert("RGB")
            im.thumbnail((256, 256))
            box = [max(0, xf - 0.06), max(0, yf - 0.08), 0.12, 0.16]
            s, whole = coach._sharpness(im, box)
            return round(s / whole, 2) if whole else 0.0

        def lock_once(c):
            """One look, one try: never waits inside the camera lock (the live picture keeps running)."""
            fm = c.refresh().get(PROPS["focus_mode"])
            if fm is None or 1 not in (fm.choices or []):
                return None                                     # no MF here: nothing to lock
            if fm.value == 1:
                return True
            if fm.writable and fm.enabled:
                c.try_set(PROPS["focus_mode"], 1)
            return False

        def lock_bg(gen: int, target: str, found: bool, t0: float):
            """Into MF in the background: the camera keeps focus mode blocked ~2.5 s after a half press."""
            ok, end = False, time.time() + 8.0
            while time.time() < end:
                if focus_job["gen"] != gen:
                    return                                      # a newer tap took over
                try:
                    r = studio.run(lock_once)
                except Exception:  # noqa: BLE001
                    r = False
                if r is None or r:
                    ok = bool(r)
                    break
                time.sleep(0.2)
            if focus_job["gen"] != gen:
                return
            focus_job["lock_ms"] = int((time.time() - t0) * 1000)
            if found:
                focus_state.update(text=f"{target}, locked" if ok else "Focused, but the camera would not lock. Tap again.",
                                   locking=False, found=ok, t=time.time())
            else:
                focus_state.update(locking=False, t=time.time())

        def work():
            t0 = time.time()
            tm = {}
            focus_job["gen"] += 1
            gen = focus_job["gen"]
            before = sharp_at(studio.frame or live_frame())

            def focus(c):
                props = c.refresh()
                fm = props.get(PROPS["focus_mode"])
                if fm is not None and fm.value == 1:            # leave MF so the camera can focus
                    want = 2 if 2 in fm.choices else (32772 if 32772 in fm.choices else None)
                    if want:
                        c.set("focus_mode", want)
                # the camera's focus indication never changes over the PC link, so the half press is held for a fixed
                # time: 0.6 s gave the same sharpness as 1.2 s on his camera (12 Nov 2026), checked below on the picture
                return c.focus_at(xf, yf, max_wait=float(body.get("max_wait", 0.6)))
            af = studio.run(focus)
            tm["focus"] = int((time.time() - t0) * 1000)
            seq = studio.frame_seq
            f, _s = studio.latest_frame(seq + 1, timeout=1.2)  # a fresh frame after the half press
            after = sharp_at(f or live_frame())
            tm["verified"] = int((time.time() - t0) * 1000)
            found = bool(af.get("acquired")) and after >= max(0.35, before * 0.85)
            why = "" if found else ("the camera did not focus" if not af.get("acquired")
                                    else "too dark or too little detail there" if after < 0.35 else "it got softer there")
            face = None
            try:
                m = measure.analyze(f or studio.frame, log=False)
                fb = (m.get("face") or {}).get("box") if (m.get("face") or {}).get("method") == "face detection" else None
                if fb and fb[0] <= xf <= fb[0] + fb[2] and fb[1] <= yf <= fb[1] + fb[3]:
                    face = fb
            except Exception:  # noqa: BLE001
                pass
            target = "your face" if face else ("the centre" if abs(xf - 0.5) < 0.08 and abs(yf - 0.5) < 0.08 else "where you tapped")
            focus_state.update(text=f"{target}: sharp, locking…" if found else f"not found ({why})", point=[xf, yf],
                               tracking=False, t=time.time(), sharp=after, found=found, locking=True)
            threading.Thread(target=lock_bg, args=(gen, target, found, t0), daemon=True).start()
            ms = int((time.time() - t0) * 1000)
            changelog.add("you", "focus", f"Focus {'found' if found else 'not found'} at {xf:.2f}, {yf:.2f}",
                          f"sharpness there {before} → {after}, {ms} ms")
            return {"ok": True, "found": found, "why": why, "target": target, "sharp_before": before, "sharp_after": after,
                    "point": [xf, yf], "ms": ms, "af": af, "locking": True, "timing": tm}
        try:
            return JSONResponse(await anyio.to_thread.run_sync(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    async def focus_track(_: Request):
        """Hand focus to the camera's face/eye tracking (AF-C, Wide)."""
        def work(c):
            props = c.refresh()
            if 32772 in props[PROPS["focus_mode"]].choices:
                c.set("focus_mode", 32772)
            area = props.get(SP_FOCUS_AREA)
            if area is not None and 1 in area.choices:
                c.set(SP_FOCUS_AREA, 1)
        try:
            await anyio.to_thread.run_sync(lambda: studio.run(work))
            focus_state.update(text="your eyes, tracking", point=None, tracking=True, t=time.time())
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    def focus_view(_: Request):
        try:
            props = studio.props(max_age=2)                     # the real mode, read back (shared, at most every 2 s)
        except Exception as exc:  # noqa: BLE001               # camera not reachable: the same answer as /api/settings
            return err(exc)
        fm = props.get(PROPS["focus_mode"])
        mode = {1: "MF (locked)", 2: "AF-S", 32772: "AF-C", 32773: "AF-A", 32774: "DMF"}.get(fm.value if fm else None, "")
        st = dict(focus_state)
        # tell the truth: the label follows what the camera is really doing, not what Studio last set
        if fm and fm.value == 32772 and not st.get("tracking"):
            st.update(text="continuous AF (set on the camera, not locked)", point=None)
        elif fm and fm.value == 1 and st.get("tracking"):
            st.update(text="", tracking=False)
        elif fm and fm.value != 32772 and st.get("tracking"):
            st.update(text="", tracking=False)
        return JSONResponse({**st, "mode": mode})

    async def focus_step(request: Request):
        body = await request.json()
        autof.manual("focus_mode")
        step = int(body.get("step", 0))

        def work(c):
            mode = c.refresh().get(PROPS["focus_mode"])
            if mode is not None and mode.value not in (1, 32774):   # needs MF or DMF
                c.set("focus_mode", 1)
            c.focus_step(step)
        try:
            await anyio.to_thread.run_sync(lambda: studio.run(work))
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return err(exc, 400)

    def autofocus(_: Request):
        """The focus button: acquire once on the current target (eyes / tapped point / centre), then lock."""
        try:
            refocus_target()
            return JSONResponse({"ok": True, "focus": focus_state})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def refocus_target() -> None:
        x, y = focus_state.get("point") or [0.5, 0.5]
        frame = studio.frame
        if frame is not None and not focus_state.get("point"):
            mf = measure.analyze(frame)
            fb = (mf.get("face") or {}).get("box") if (mf.get("face") or {}).get("method") == "face detection" else None
            if fb:
                x, y = fb[0] + fb[2] / 2, fb[1] + fb[3] * 0.4

        def work(c):
            props = c.refresh()
            fm = props.get(PROPS["focus_mode"])
            if fm is not None and 2 in fm.choices:
                c.set("focus_mode", 2)
            area = props.get(SP_FOCUS_AREA)
            spot = next((a for a in (259, 258, 257) if area is not None and a in area.choices), None)
            if spot is not None and area.value != spot:
                c.set(SP_FOCUS_AREA, spot)
            c.focus_at(x, y)
        studio.run(work)
        time.sleep(0.7)
        studio.run(lambda c: c.set("focus_mode", 1) if 1 in c.refresh()[PROPS["focus_mode"]].choices else None)
        focus_state.update(point=[x, y], tracking=False, t=time.time(),
                           text=focus_state.get("text") if "locked" in (focus_state.get("text") or "") else "locked on the target")
        changelog.add("you", "focus", f"Focus acquired and locked at {x:.2f}, {y:.2f}", "")

    def shoot(_: Request):
        try:
            return JSONResponse(take_photo())
        except OSError as exc:                                 # the laptop side: disk full, folder gone
            if getattr(exc, "errno", None) == 28:
                msg = "The laptop's disk is full, so the photo could not be saved here. It is still on the card; free some space."
            else:
                msg = "The photo could not be saved on the laptop. It is still on the card."
            return JSONResponse({"error": msg}, status_code=507 if getattr(exc, "errno", None) == 28 else 500)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def take_photo() -> dict:
        """Shoot one photo (only ever from the owner's button or the smart mode he armed). RAW is enforced first."""
        moment = studio.reel.snapshot() if studio.reel.on and not studio.recording else []   # the 3 s before his press
        fmt = enforce_format("photo")
        long_s = play["long_s"] if play["long_on"] and play["long_before"] is not None else None
        if long_s:                               # his long exposure: the shutter is open, then the camera finishes
            from .drivers.sony import S1_SETTLE_S
            play.update(long_phase="open", long_open_until=time.time() + S1_SETTLE_S + long_s)
        try:                                     # no AF on the shutter: the focus lock holds
            files = studio.run(lambda c: c.capture(af=False, timeout=2 * long_s + 40) if long_s else c.capture(af=False))
        finally:
            play.update(long_phase=None, long_open_until=0.0)      # no retry, no second press by Studio
        saved = save_files(files)
        if not saved:                         # nothing arrived: say so in words, and log no empty take
            return {"saved": [], "shoot": [], "take": None, "format_fixed": fmt, "failed": True,
                    "note": "The camera did not take the picture (nothing arrived from it). Check the card and press again."}
        shoot_copies = []
        for name in saved:                       # copy into the current shoot folder (originals stay)
            try:
                shoot_copies.append(str(handoff.add_file(CAPTURE_DIR / name)))
            except OSError:
                pass
        dbl = None
        try:
            dbl = _double_after_photo(shoot_copies)
        except Exception:  # noqa: BLE001 - the photo itself is saved whatever happens to the blend
            dbl = {"say": "The double exposure could not be made. Both photos are saved as they are."}
        if moment:                               # the clip is written in the background: the photo is not slower
            _reel_clip_later(moment, shoot_copies or [str(CAPTURE_DIR / n) for n in saved])
        take = None
        try:
            jpg = next((CAPTURE_DIR / n for n in saved if n.lower().endswith(".jpg")), None)
            take = record_take("photo", time.time(), time.time(), jpg.read_bytes() if jpg else studio.frame, shoot_copies)
        except Exception:  # noqa: BLE001
            pass
        note = ""
        if len(shoot_copies) < len(saved):
            note = ("The photo is saved, but it could not be copied into the shoot folder (disk full or folder missing). "
                    "It is in Studio's captures folder and on the card.")
        return {"saved": saved, "shoot": shoot_copies, "take": take["id"] if take else None, "format_fixed": fmt,
                "note": note, "double": dbl}

    async def record(request: Request):
        """Start or stop recording: a dedicated start and a dedicated stop. The camera's movie button is a toggle,
        so Studio decides from its own intent: a stop can never send a start, a start is only sent when Studio is
        clearly not recording, and nothing is sent while the state is being checked. Every press is logged."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        from .drivers.sony import _clog
        action = body.get("action") or ("stop" if studio.recording else "start")
        if action not in ("start", "stop"):
            return JSONResponse({"error": "unknown action"}, status_code=400)
        if prefs().get("record_to") == "laptop" and getattr(studio, "card", None) is not None:
            # "Record video to: This laptop": his press records the card's picture and sound; the camera's own
            # recording is never started or stopped by this
            try:
                return JSONResponse(await anyio.to_thread.run_sync(lambda: _card_record(action)))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
        if time.time() - studio.rec_toggled < 2.5:
            return JSONResponse({"error": "Wait a moment between start and stop."}, status_code=429)
        if action == "start" and studio.rec_check is not None:
            _clog("movie start refused", why="checking the camera")
            return JSONResponse({"error": "Checking the camera… press record again in a moment.", "checking": True},
                                status_code=409)
        if (action == "start") == studio.recording:            # already in that state: send nothing
            _clog(f"movie {action} not sent", why="already " + ("recording" if studio.recording else "stopped"))
            return JSONResponse({"ok": True, "recording": studio.recording, "elapsed": 0, "format_fixed": [],
                                 "camera_state": None, "note": "", "sent": False})
        want = action == "start"
        studio.rec_note = ""
        fmt = []
        if want:
            try:
                fmt = enforce_format("video")             # S-Log3 + best quality before every take
            except Exception:  # noqa: BLE001
                fmt = []

        def work(c):
            p0 = c.refresh()
            left0 = _num(p0, PROP_REC_TIME_LEFT)
            c.movie_record(True)
            seen, left = None, left0
            for _ in range(24):                      # up to ~6 s for the camera to confirm (flag or card time)
                time.sleep(0.25)
                p = c.refresh()
                seen, left = _num(p, PROP_REC_STATE), _num(p, PROP_REC_TIME_LEFT)
                if seen is not None and bool(seen) == want:
                    break
            _clog(f"movie {action} sent by his press", flag=seen, left_before=left0, left_after=left)
            return seen
        try:
            seen = await anyio.to_thread.run_sync(lambda: studio.run(work))
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        studio.recording = want
        studio.rec_toggled = time.time()
        studio.rec_since = time.time() if want else 0.0
        p = prefs()
        try:
            if want:
                studio.take_start = time.time()
                devs = video.capture_devices() if p.get("hdmi") else []
                vjobs.recording_started(devs[0] if devs else None)
            else:
                vjobs.recording_stopped({"log": True})
                t = record_take("video", getattr(studio, "take_start", time.time()), time.time(), studio.frame, [])
                if t:
                    vjobs.status["take"] = t["id"]
        except Exception:  # noqa: BLE001 - never let the hand-off break recording
            pass
        note = ""
        if seen is not None and bool(seen) != want:
            note = ("The camera hasn't confirmed " + ("recording" if want else "stopping") +
                    " yet; check the camera screen (card inserted? not overheating?).")
        studio.rec_check = None
        return JSONResponse({"ok": True, "recording": studio.recording, "elapsed": 0, "format_fixed": fmt,
                             "camera_state": seen, "note": note, "sent": True})

    def disconnect(_: Request):
        studio.shutdown()
        return JSONResponse({"ok": True})

    logged_note = {"text": ""}
    alt_cache: dict = {}

    def health(_: Request):
        if studio.waiting_note and studio.waiting_note != logged_note["text"]:
            logged_note["text"] = studio.waiting_note
            autof._event(studio.waiting_note, kind="camera")         # shows in History and as a notice
        h = studio.health()
        h["route"] = "usb" if studio.host == "usb" else "wifi"
        h["choice"] = studio.choice
        alt = {}
        if h.get("state") in ("absent", "unresponsive"):
            # where else is the camera? so the screen can say it plainly and offer the way back (cached 5 s)
            now = time.time()
            if now - alt_cache.get("t", 0) > 5:
                usb = None
                try:
                    usb = _camera_on_usb()
                except Exception:  # noqa: BLE001
                    pass
                saved = wifi.load().get("host")
                wf = None
                if saved and saved != "usb":
                    try:
                        wf = wifi.reachable(saved, timeout=0.5)
                    except Exception:  # noqa: BLE001
                        pass
                alt_cache.update(t=now, usb_present=usb, wifi_answers=wf, wifi_host=saved)
            alt = {k: alt_cache.get(k) for k in ("usb_present", "wifi_answers", "wifi_host")}
        return JSONResponse({**h, "alt": alt, "fps": round(studio.fps, 1), "waiting_note": studio.waiting_note,
                             "mode": "usb" if studio.host == "usb" else "wifi", "host": studio.host})

    async def connection(request: Request):
        """GET: current connection. POST {mode: usb|wifi, host?}: switch (session closed cleanly first)."""
        if request.method == "POST":
            body = await request.json()
            try:
                if body.get("choice") == "auto":
                    studio.set_choice("auto")
                else:
                    await anyio.to_thread.run_sync(lambda: studio.set_connection(
                        "wifi" if body.get("mode") == "wifi" else "usb", (body.get("host") or "").strip() or None))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        cfg = wifi.load()
        return JSONResponse({"mode": "usb" if studio.host == "usb" else "wifi", "host": studio.host, "choice": studio.choice,
                             "route_at": studio.route_at,
                             "camera": studio.cam.name if studio.cam else "", "health": studio.health(),
                             "saved_host": cfg.get("host"), "saved_name": cfg.get("name", ""),
                             "client_name": wifi.client_name()})

    async def wifi_discover(_: Request):
        return JSONResponse(await anyio.to_thread.run_sync(wifi.discover))

    def shutdown(request: Request):
        """Stop Studio cleanly (closes the camera session first). This PC only."""
        if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
            return JSONResponse({"error": "only from this PC"}, status_code=403)
        studio.shutdown()
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return JSONResponse({"ok": True})

    def gallery(_: Request):
        files = sorted(CAPTURE_DIR.glob("*.jpg"), reverse=True)[:24] if CAPTURE_DIR.exists() else []
        return JSONResponse([f.name for f in files])

    def capture_file(request: Request):
        name = request.path_params["name"]
        path = (CAPTURE_DIR / name).resolve()
        if path.parent != CAPTURE_DIR.resolve() or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path)

    _app = Starlette(routes=[
        Route("/", index),
        Route("/api/catalog", catalog),
        Route("/api/preset", preset, methods=["POST"]),
        Route("/api/autopilot", autopilot, methods=["POST"]),
        Route("/api/follow", follow, methods=["GET", "POST"]),
        Route("/api/record/state", record_state),
        Route("/api/auto/start", auto_start, methods=["POST"]),
        Route("/api/auto/stop", auto_stop, methods=["POST"]),
        Route("/api/auto/status", auto_status),
        Route("/api/series/start", series_start, methods=["POST"]),
        Route("/api/series/status", series_status),
        Route("/api/series/plan", series_plan, methods=["POST"]),
        Route("/api/series/cancel", series_cancel, methods=["POST"]),
        Route("/api/match/ref", match_ref, methods=["POST"]),
        Route("/api/match", match_now),
        Route("/api/light", light_view),
        Route("/api/camera/check", camera_check, methods=["GET", "POST"]),
        Route("/api/camera/log", camera_log, methods=["POST"]),
        Route("/api/zoom", zoom_view, methods=["GET", "POST"]),
        Route("/api/claude/state", claude_state),
        Route("/api/ready", ready_view),
        Route("/api/camera/report", camera_report, methods=["POST"]),
        Route("/api/camera/report/open", camera_report_open, methods=["POST"]),
        Route("/api/camera/report/notnow", camera_report_notnow, methods=["POST"]),
        Route("/api/claude/key", claude_key, methods=["GET", "POST"]),
        Route("/api/claude/direct", claude_direct, methods=["POST"]),
        Route("/api/claude/review", claude_review, methods=["POST"]),
        Route("/api/claude/best", claude_best, methods=["POST"]),
        Route("/api/claude/apply", claude_apply, methods=["POST"]),
        Route("/api/props", all_props),
        Route("/api/prop", set_prop, methods=["POST"]),
        Route("/api/focus/point", focus_point, methods=["POST"]),
        Route("/api/focus/track", focus_track, methods=["POST"]),
        Route("/api/focus", focus_view),
        Route("/api/focus/step", focus_step, methods=["POST"]),
        Route("/api/lut/{key}.cube", lut_export),
        Route("/api/liveview.jpg", liveview),
        Route("/api/settings", settings),
        Route("/api/formats", formats_view, methods=["GET", "POST"]),
        Route("/api/sound/ask", sound_ask, methods=["POST"]),
        Route("/api/art/{key}.jpg", card_art),
        Route("/api/looks", looks_list),
        Route("/api/looks/sample/{key}.jpg", looks_sample),
        Route("/api/looks/table/{key}", looks_table),
        Route("/api/looks/still", looks_still),
        Route("/api/looks/copy", looks_copy, methods=["POST"]),
        Route("/api/looks/stop", looks_stop, methods=["POST"]),
        Route("/api/card", card_view, methods=["GET", "POST"]),
        Route("/api/card/levels", card_levels),
        Route("/api/play", play_view, methods=["GET", "POST"]),
        Route("/api/play/image/{which}.jpg", play_image),
        Route("/api/play/long/apply", long_apply, methods=["POST"]),
        Route("/api/play/long/restore", long_restore, methods=["POST"]),
        Route("/api/play/paint/save", paint_save, methods=["POST"]),
        Route("/api/play/mood", mood_ask, methods=["POST"]),
        Route("/api/play/mood/back", mood_back, methods=["POST"]),
        Route("/api/play/pull/mark", pull_mark, methods=["POST"]),
        Route("/api/play/pull/run", pull_run, methods=["POST"]),
        Route("/api/reel", reel_view, methods=["GET", "POST"]),
        Route("/api/reel/make", reel_make, methods=["POST"]),
        Route("/api/reel/stop", reel_stop, methods=["POST"]),
        Route("/api/reel/delete", reel_delete, methods=["POST"]),
        Route("/api/reel/show", reel_show, methods=["POST"]),
        Route("/api/sound/cancel", sound_cancel, methods=["POST"]),
        Route("/api/claude/model", claude_model, methods=["POST"]),
        Route("/api/claude/context", claude_context, methods=["POST"]),
        Route("/api/takes/{tid:int}/sound", take_sound, methods=["POST"]),
        Route("/api/set", set_setting, methods=["POST"]),
        Route("/api/autofocus", autofocus, methods=["POST"]),
        Route("/api/shoot", shoot, methods=["POST"]),
        Route("/api/record", record, methods=["POST"]),
        Route("/api/disconnect", disconnect, methods=["POST"]),
        Route("/api/gallery", gallery),
        Route("/captures/{name}", capture_file),
        Route("/api/autofix", autofix_view, methods=["GET", "POST"]),
        Route("/api/post", post_view, methods=["GET", "POST"]),
        Route("/api/shoot", shoot_view),
        Route("/api/takes", takes_view),
        Route("/api/takes/{tid:int}/match", take_match, methods=["POST"]),
        Route("/api/takes/{tid:int}/claude", take_claude, methods=["POST"]),
        Route("/api/plan", plan_view, methods=["GET", "POST"]),
        Route("/api/smart", smart_view, methods=["GET", "POST"]),
        Route("/api/verdict", verdict_view),
        Route("/api/ui/log", ui_log, methods=["GET", "POST"]),
        Route("/api/usage", usage_view, methods=["GET", "POST"]),
        Route("/api/wb/measure", wb_measure, methods=["POST"]),
        Route("/api/wb/auto", wb_auto, methods=["POST"]),
        Route("/api/wb/clear", wb_clear, methods=["POST"]),
        Route("/api/plan/current", plan_current, methods=["POST"]),
        Route("/api/plan/setup", plan_setup, methods=["POST"]),
        Route("/api/video/status", video_status),
        Route("/api/video/cancel", video_cancel, methods=["POST"]),
        Route("/api/video/script", video_script, methods=["POST"]),
        Route("/api/shoot/new", shoot_new, methods=["POST"]),
        Route("/api/shoot/open", shoot_open, methods=["POST"]),
        Route("/api/shoot/export", shoot_export, methods=["POST"]),
        Route("/api/shoot/resolve", shoot_resolve, methods=["POST"]),
        Route("/api/photoshop", photoshop, methods=["POST"]),
        Route("/api/rawreview/{name}", raw_review),
        Route("/api/camcheck", camcheck_view, methods=["GET", "POST"]),
        Route("/api/diag/extinfo", ext_info),
        Route("/manifest.webmanifest", manifest_view),
        Route("/icon-{size:int}.png", icon_view),
        Route("/api/client_config", client_config),
        Route("/api/diag/fetch_waiting", fetch_waiting, methods=["POST"]),
        Route("/api/diag/shutter", shutter_variant, methods=["POST"]),
        Route("/api/coach", coach_view),
        Route("/api/coach/fix", coach_fix, methods=["POST"]),
        Route("/api/simple/auto", simple_auto, methods=["POST"]),
        Route("/api/setups", setups_list),
        Route("/api/setups/save", setups_save, methods=["POST"]),
        Route("/api/setups/{sid}", setups_get),
        Route("/api/setups/undo", setups_undo, methods=["POST"]),
        Route("/api/nd", nd_view, methods=["GET", "POST"]),
        Route("/api/lineup", lineup_view, methods=["GET", "POST"]),
        Route("/api/setups/{sid}/recall", setups_recall, methods=["POST"]),
        Route("/api/setups/{sid}/update", setups_update, methods=["POST"]),
        Route("/api/setups/{sid}/delete", setups_delete, methods=["POST"]),
        Route("/api/gear", gear, methods=["GET", "POST"]),
        Route("/api/health", health),
        Route("/api/connection", connection, methods=["GET", "POST"]),
        Route("/api/wifi/discover", wifi_discover, methods=["POST"]),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/director/start", director_start, methods=["POST"]),
        Route("/api/director/stop", director_stop, methods=["POST"]),
        Route("/api/director/status", director_status),
        Route("/api/director/undo", director_undo, methods=["POST"]),
        Route("/api/changes/apply", changes_apply, methods=["POST"]),
        Route("/api/lighting/plan", lighting_plan, methods=["POST"]),
        Route("/api/lighting/compare", lighting_compare, methods=["POST"]),
    ])

    class _Touch:
        """Any action he takes in the app (a POST) means: he is working, Studio-initiated camera writes may follow."""
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("method") == "POST" and scope.get("path") not in QUIET_POSTS:
                autof.touch()
                autof.studio_busy(20)
            await self.inner(scope, receive, send)

    class _Perf:
        """Counts requests per path and their duration (for the "less laggy" measurement; numbers only)."""
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                return await self.inner(scope, receive, send)
            t0 = time.perf_counter()
            try:
                await self.inner(scope, receive, send)
            finally:
                _perf_add("http", scope.get("method", "") + " " + scope.get("path", ""), (time.perf_counter() - t0) * 1000)

    def perf_view(request: Request):
        if not (request.client and (request.client.host or "").startswith("127.")):
            return JSONResponse({"error": "laptop only"}, status_code=403)
        out = {k: {n: {"n": v[0], "ms_avg": round(v[1] / max(1, v[0]), 1), "ms_max": round(v[2], 1), "wait_avg": round(v[3] / max(1, v[0]), 1)}
                   for n, v in sorted(d.items(), key=lambda kv: -kv[1][1])} for k, d in PERF.items()}
        out["since_s"] = round(time.time() - PERF_T[0], 1)
        out["fps"] = round(studio.fps, 1)
        if request.query_params.get("reset"):
            PERF.clear(); PERF_T[0] = time.time()
        return JSONResponse(out)
    class _AnsweredBy:
        """When an answer came from the safety-net model during this request, the page is told (one header)."""
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http" or scope.get("method") != "POST":
                return await self.inner(scope, receive, send)
            n0 = len(assistant.FALLBACKS)
            t0 = assistant.FALLBACKS[-1][0] if assistant.FALLBACKS else 0

            async def send2(msg):
                if msg.get("type") == "http.response.start" and assistant.FALLBACKS and \
                        (len(assistant.FALLBACKS) != n0 or assistant.FALLBACKS[-1][0] != t0):
                    msg = {**msg, "headers": list(msg.get("headers") or []) +
                           [(b"x-claude-answered-by", assistant.FALLBACK_NAME.encode())]}
                await send(msg)
            await self.inner(scope, receive, send2)

    _app.router.routes.append(Route("/api/diag/perf", perf_view))
    _app.add_middleware(_AnsweredBy)
    _app.add_middleware(_Touch)
    _app.add_middleware(_Perf)
    return _app


PERF: dict = {}
PERF_T = [time.time()]


def _perf_add(kind: str, name: str, ms: float, wait_ms: float = 0.0) -> None:
    d = PERF.setdefault(kind, {})
    v = d.setdefault(name, [0, 0.0, 0.0, 0.0])
    v[0] += 1
    v[1] += ms
    v[2] = max(v[2], ms)
    v[3] += wait_ms


QUIET_POSTS = {"/api/autofix", "/api/shutdown"}


# (the old classic page was removed 12 Nov 2026: Studio has one layout)



# ---------------------------------------------------------------- LAN + PIN --
PIN_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "camctl" / "studio_pin.json"
SESSION_COOKIE = "camctl_session"


def _hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000).hex()


def set_pin(pin: str) -> None:
    salt = secrets.token_bytes(16)
    PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIN_FILE.write_text(json.dumps({"salt": salt.hex(), "hash": _hash_pin(pin, salt)}))


def check_pin(pin: str) -> bool:
    try:
        data = json.loads(PIN_FILE.read_text())
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(_hash_pin(pin, bytes.fromhex(data["salt"])), data["hash"])


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>camctl Studio</title>
<style>body{background:#111316;color:#e8eaed;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0}
form{background:#1b1e23;padding:24px;border-radius:12px;display:grid;gap:12px;width:260px}
input,button{padding:12px;border-radius:8px;border:1px solid #2a2f36;font-size:18px}
input{background:#0f1114;color:#e8eaed;text-align:center;letter-spacing:4px}button{background:#4f8cff;color:#fff;border:0}
p{color:#e5484d;margin:0;min-height:20px}</style></head><body>
<form method="post" action="/login"><strong>camctl Studio</strong>
<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN" autofocus>
<button>Unlock</button><p>%MSG%</p></form></body></html>"""


def _private_address(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


LAN_STATE: dict = {"on": False, "addresses": [], "pin": False}   # iPad / phone access (status for All settings)


class PinGate:
    """ASGI middleware: in LAN mode every route except /login needs a session
    cookie obtained by entering the PIN. 5 wrong PINs lock logins for 60 s."""

    def __init__(self, app):
        self.app = app
        self.sessions: set[str] = set()
        self.failures = 0
        self.locked_until = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        client = (scope.get("client") or ("", 0))[0]
        if not _private_address(client):            # home network only, never the internet
            return await JSONResponse({"error": "private network only"}, status_code=403)(scope, receive, send)
        if client in ("127.0.0.1", "::1"):              # the laptop itself: its own window and launchers, no PIN
            return await self.app(scope, receive, send)
        if scope["path"] == "/login":
            return await self.login(request)(scope, receive, send)
        if request.cookies.get(SESSION_COOKIE) in self.sessions:
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/api/"):
            return await JSONResponse({"error": "locked"}, status_code=401)(scope, receive, send)
        return await RedirectResponse("/login", status_code=303)(scope, receive, send)

    def login(self, request: Request):
        async def handler(scope, receive, send):
            if request.method != "POST":
                return await HTMLResponse(LOGIN_PAGE.replace("%MSG%", ""))(scope, receive, send)
            if time.time() < self.locked_until:
                msg = "Too many tries. Wait a minute."
                return await HTMLResponse(LOGIN_PAGE.replace("%MSG%", msg), status_code=429)(scope, receive, send)
            form = await request.form()
            if check_pin(str(form.get("pin", ""))):
                self.failures = 0
                token = secrets.token_urlsafe(32)
                self.sessions.add(token)
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400)
                return await resp(scope, receive, send)
            self.failures += 1
            if self.failures >= 5:
                self.failures, self.locked_until = 0, time.time() + 60
            return await HTMLResponse(LOGIN_PAGE.replace("%MSG%", "Wrong PIN"), status_code=401)(scope, receive, send)
        return handler


def lan_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if not i[4][0].startswith("127.")})


_CTRL_HANDLER = None   # keep a reference so Windows can call it


def _close_on_exit(studio: Studio) -> None:
    """End the camera's PTP session however Studio stops: normal exit, Ctrl+C, or the
    console window being closed. A session left open mid-transfer can freeze the
    camera's USB link until it is switched off and on."""
    import atexit
    import signal
    atexit.register(studio.shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: (studio.shutdown(), os._exit(0)))
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            def handler(ctrl_type):
                if ctrl_type in (2, 5, 6):        # close window, logoff, shutdown
                    studio.shutdown()
                return False                      # let Windows / uvicorn carry on
            global _CTRL_HANDLER
            _CTRL_HANDLER = handler
            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="camctl Studio: visual camera control")
    ap.add_argument("--camera", default=None,
                    help='"usb" or the camera IP for Wi-Fi (default: the last connection chosen in Studio)')
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--lan", action="store_true",
                    help="also serve on your home network (iPad/phone), protected by a PIN")
    ap.add_argument("--set-pin", action="store_true", help="choose a new PIN and exit")
    args = ap.parse_args()

    if args.set_pin or (args.lan and not PIN_FILE.exists()):
        import getpass
        while True:
            pin = getpass.getpass("Choose a PIN for camctl Studio (at least 6 digits): ").strip()
            if len(pin) >= 6 and pin == getpass.getpass("Repeat the PIN: ").strip():
                set_pin(pin)
                print("PIN saved.")
                break
            print("PINs didn't match or were shorter than 6 characters. Try again.")
        if args.set_pin:
            return

    if args.camera is None:
        cfg = wifi.load()
        args.camera = cfg["host"] if cfg.get("mode") == "wifi" and cfg.get("host") else "usb"
    studio = Studio(args.camera)
    _close_on_exit(studio)
    app = build_app(studio, start_auto=True)
    host = "127.0.0.1"
    if args.lan:
        app, host = PinGate(app), "0.0.0.0"
        LAN_STATE.update(on=True, addresses=[f"http://{ip}:{args.port}" for ip in lan_addresses()], pin=PIN_FILE.exists())
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"camctl Studio on {url}")
    if args.lan:
        for ip in lan_addresses():
            print(f"  on your iPad/phone (same Wi-Fi): http://{ip}:{args.port}  (PIN required)")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
