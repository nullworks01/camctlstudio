"""Which driver talks to which camera, from the per-camera data in cameras.json.

Order of choice for a camera on the USB cable:
  1. an official maker SDK adapter, if the user registered one on their own computer (register_sdk; none ship here),
  2. Studio's own driver for the maker (Sony, Canon EOS, Nikon, or plain PTP for other makers),
  3. the optional gphoto2 route (Linux / macOS, only if the gphoto2 package is already installed and the user chose it).
A Sony on the cable always gets the Sony driver, exactly as before: the tested path does not change.
Nothing here installs anything, changes a driver, or shoots.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).with_name("cameras.json")
SONY_VID = 0x054C

# brand -> factory(host) returning a driver; filled only by a user's own adapter (see docs/cameras.md)
SDK_ADAPTERS: dict[str, object] = {}


def register_sdk(brand: str, factory) -> None:
    """Plug-in point for an official SDK (Sony Camera Remote SDK, Canon EDSDK, Nikon SDK). Their licences do not
    allow shipping them with this MIT project, so an adapter lives only on the computer of the user who registered
    with the maker. factory(host) must return an object with the CameraDriver methods (drivers/base.py)."""
    SDK_ADAPTERS[brand] = factory


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def brands() -> dict[int, str]:
    return {int(v["vendor_id"], 16): k for k, v in data()["brands"].items()}


def brand_info(brand: str | None) -> dict:
    return data()["brands"].get(brand or "", {})


def driver_name(brand: str | None) -> str | None:
    if brand in SDK_ADAPTERS:
        return "sdk"
    return brand_info(brand).get("driver")


def controllable_vendors() -> set[int]:
    return {vid for vid, b in brands().items() if brand_info(b).get("driver")}


def model_info(brand: str | None, model: str | None) -> dict:
    for m in data()["models"]:
        if m["brand"] == brand and m["model"] == model:
            return m
    return {}


def driver_class(name: str | None):
    if name == "sony":
        from .sony import SonyCamera
        return SonyCamera
    if name == "canon":
        from .canon import CanonEOSCamera
        return CanonEOSCamera
    if name == "nikon":
        from .nikon import NikonCamera
        return NikonCamera
    if name == "ptp-generic":
        from .ptpstd import PTPCamera
        return PTPCamera
    if name == "gphoto2":
        from .gphoto import Gphoto2Camera
        return Gphoto2Camera
    return None


def usb_cameras() -> list[tuple[int, int]]:
    """(vendor id, product id) of every PTP camera on the USB bus whose maker Studio can drive."""
    try:
        from ..ptpusb import find_ptp_cameras
        devs = find_ptp_cameras(None)
    except Exception:  # noqa: BLE001 - no pyusb / no libusb: nothing found
        return []
    ok = controllable_vendors()
    return [(d.idVendor, d.idProduct) for d in devs if d.idVendor in ok]


def any_on_usb() -> bool:
    return bool(usb_cameras())


def make_usb_driver(prefer: str | None = None, sony=None):
    """The driver for the camera on the cable. With no camera found, the Sony driver (its messages explain the
    setup, as before). prefer="gphoto2" uses the optional gphoto2 route when it is installed."""
    if prefer == "gphoto2":
        from . import gphoto
        if gphoto.available():
            return gphoto.Gphoto2Camera("usb")
    found = usb_cameras()
    if sony is None:
        from .sony import SonyCamera as sony
    if any(v == SONY_VID for v, _ in found) or not found:
        return sony("usb")
    vid = found[0][0]
    brand = brands()[vid]
    if brand in SDK_ADAPTERS:
        return SDK_ADAPTERS[brand]("usb")
    cls = driver_class(driver_name(brand))
    if cls is None:
        return sony("usb")
    drv = cls("usb", vendor=vid) if cls.__name__ != "Gphoto2Camera" else cls("usb")
    drv.brand = drv.brand if drv.brand != "Camera" else brand
    return drv
