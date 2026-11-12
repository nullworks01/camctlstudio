"""What Studio needs from a camera, in one place: the plug-in point for a second brand.

Only Sony is implemented (camctl.drivers.sony.SonyCamera). Another brand would provide a class with these methods and
be added to DRIVERS; nothing else in Studio should need to know the brand. Brands Studio cannot talk to are recognised
by their USB vendor id only, so the user hears a plain sentence instead of a silent failure. No fake support.
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


# USB vendor ids of camera makers (public USB-IF ids): recognised, not supported unless a driver exists
BRANDS = {0x054C: "Sony", 0x04A9: "Canon", 0x04B0: "Nikon", 0x04CB: "Fujifilm", 0x04DA: "Panasonic",
          0x07B4: "OM System / Olympus", 0x25FB: "Ricoh / Pentax", 0x1A98: "Leica", 0x2672: "GoPro", 0x0A12: "Blackmagic"}
SUPPORTED = {"Sony"}
TESTED = {("Sony", "ZV-E10"): "2.03"}                          # brand, model -> firmware tested


def brand_of(vendor_id: int | None) -> str | None:
    return BRANDS.get(vendor_id) if vendor_id is not None else None


def guide(brand: str | None, model: str | None, connected: bool, route: str | None = None) -> dict:
    """What to tell the user about this camera, in plain words. kind: tested / untested / unsupported / unknown."""
    if brand and brand not in SUPPORTED:
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
        return {"kind": "untested", "title": f"Sony {model or 'camera'}: untested",
                "text": ("This Sony model is untested. Turn on PC Remote (MENU › Network › PC Remote Function) and use "
                         "\"Check this camera\" in All settings › Studio: it tests what works without taking a photo."),
                "steps": ["MENU › Network › PC Remote Function › PC Remote: On.", "Then: All settings › Studio › Check this camera."]}
    return {"kind": "unknown", "title": "Camera", "text": "", "steps": []}
