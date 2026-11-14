"""What Studio needs from a camera, in one place: the plug-in point for a second brand.

Drivers: Sony (tested on a ZV-E10), Canon EOS, Nikon and plain PTP for other makers (all untested), and the optional
gphoto2 route; registry.py picks one per camera from cameras.json. Each driver provides these methods and translates its
camera's values into Studio's units, so nothing else in Studio needs to know the brand. Brands no driver can talk to
are recognised by their USB vendor id only, so the user hears a plain sentence instead of a silent failure.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CameraDriver(Protocol):
    """The functions Studio calls on a camera."""
    name: str

    def connect(self) -> None: ...                              # open the session (USB or Wi-Fi)
    def close(self) -> None: ...
    def refresh(self) -> dict: ...                              # read every value: {code: PropInfo}
    def set(self, prop, value) -> None: ...                     # set a value and wait for the read-back
    def live_view(self) -> bytes: ...                           # one live-view JPEG
    def capture(self, timeout: float = 20.0, af: bool = True) -> list: ...   # half press, shutter, files
    def movie_record(self, start: bool) -> None: ...            # the movie button
    def zoom(self, direction: int) -> None: ...                 # -1 wider, 0 stop, +1 closer
    def focus_step(self, step: int) -> None: ...                # manual focus nearer (-) / farther (+)
    def focus_at(self, xf: float, yf: float, max_wait: float = 1.2, lock_in_s1: bool = False) -> dict: ...


# USB vendor ids of camera makers (public USB-IF ids). Which ones Studio can drive, and how, is in cameras.json.
from . import registry as _registry  # noqa: E402

BRANDS = _registry.brands()
SUPPORTED = {b for b in BRANDS.values() if _registry.driver_name(b)}
TESTED = {("Sony", "ZV-E10"): "2.03"}                          # brand, model -> firmware tested


def driver_ok(brand: str | None) -> bool:
    """Does Studio have a driver (tested or not) for this maker?"""
    return bool(brand and _registry.driver_name(brand))


def brand_of(vendor_id: int | None) -> str | None:
    return BRANDS.get(vendor_id) if vendor_id is not None else None


def guide(brand: str | None, model: str | None, connected: bool, route: str | None = None, usb: dict | None = None) -> dict:
    """What to tell the user about this camera, in plain words. kind: tested / untested / unsupported / unknown /
    usb_mode (on the cable, but in a USB mode that only copies files). usb: registry.lookup_usb() for the cable."""
    info = _registry.brand_info(brand)
    if usb and not usb.get("control") and not connected and _registry.driver_name(brand):
        name = f"{brand} {usb.get('model') or model or 'camera'}"
        return {"kind": "usb_mode", "title": f"{name}: switch the USB mode",
                "text": (f"The {name} is on the cable in its {usb.get('usb_mode') or 'file copy'} USB mode, which only "
                         "copies files. Set the camera's USB connection to remote control (PC Remote / PTP / tether); "
                         "Studio then connects by itself."),
                "steps": list(info.get("camera_setup") or [])}
    if brand and not _registry.driver_name(brand):
        return {"kind": "unsupported", "title": f"{brand} cameras are not supported yet",
                "text": (f"camctl Studio can't control {brand} cameras yet. Nothing was changed on your camera. "
                         "You can save a short report so support can be added."), "steps": []}
    if brand == "Sony" and model and (brand, model) in TESTED:
        steps = ["MENU › Network › PC Remote Function › PC Remote: On.",
                 "Wi-Fi: connection method Access Point (camera on your home Wi-Fi). The first time choose Pairing on the "
                 "camera and confirm.",
                 "USB: MENU › Network › PC Remote Function › USB connection: PC Remote (Windows may need a driver that "
                 "Studio never installs itself; Wi-Fi needs none).",
                 "Picture Profile PP8 left at the camera's S-Log3 default."]
        return {"kind": "tested", "title": f"Sony {model}", "text": ("Tested camera." if connected else
                "Set the camera up like this, then Studio connects by itself."), "steps": [] if connected else steps}
    if brand == "Sony":
        known = bool(model) and any(model.lower() in n.lower() or n.lower() in model.lower()
                                    for n in _registry.control_pids("Sony"))
        return {"kind": "untested", "title": f"Sony {model or 'camera'}: untested",
                "text": (("libgphoto2 lists this model as remote-controllable. " if known else "") +
                         "This Sony model is untested. Turn on PC Remote (MENU › Network › PC Remote Function) and use "
                         "\"Check this camera\" in All settings › Studio: it tests what works without taking a photo."),
                "steps": ["MENU › Network › PC Remote Function › PC Remote: On.", "Then: All settings › Studio › Check this camera."]}
    if brand:
        name = f"{brand} {model}" if model else f"{brand} camera"
        basic = info.get("driver") == "ptp-generic"
        what = ("basic control only: the settings it lists and the shutter, no live picture" if basic else
                "settings, live picture, photo, video and focus over the USB cable")
        if connected:
            text = (f"Connected. {brand} support is new and untested: {what}. Use \"Check this camera\" in "
                    "All settings › Studio to see what works, without taking a photo.")
        else:
            text = (f"Studio can control {brand} cameras over the USB cable ({what}). It has not been tested on a real "
                    f"{brand} yet. " + (info.get("windows") or ""))
        steps = list(info.get("camera_setup") or [])
        if not connected and info.get("windows"):
            steps.append(info["windows"])
        steps.append("Then: All settings › Studio › Check this camera.")
        return {"kind": "untested", "title": f"{name}: untested", "text": text.strip(), "steps": steps}
    return {"kind": "unknown", "title": "Camera", "text": "", "steps": []}


# RAW files by maker (what Studio saves, sorts into PHOTO RAW and opens in Photoshop)
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".rw2", ".orf", ".pef", ".dng"}
_TIFF_RAW = {"Sony": ".arw", "Canon": ".cr2", "Nikon": ".nef", "Ricoh / Pentax": ".pef", "Leica": ".dng"}


def file_ext(data: bytes, brand: str | None = None) -> str:
    """The file name ending for a picture the camera sent, from its first bytes (and the maker for TIFF-based RAW)."""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[4:12] == b"ftypcrx ":
        return ".cr3"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return ".heif"
    if data[:15] == b"FUJIFILMCCD-RAW":
        return ".raf"
    if data[:4] == b"IIU\x00":
        return ".rw2"
    if data[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return ".orf"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return _TIFF_RAW.get(brand or "Sony", ".dng")
    return ".bin"
