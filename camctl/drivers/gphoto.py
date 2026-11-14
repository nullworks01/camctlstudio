"""Any camera libgphoto2 knows, through the optional `gphoto2` Python package. UNTESTED ON A REAL CAMERA.

libgphoto2 (LGPL) drives several thousand cameras over USB. Studio does not ship it and never installs it: it is an
optional route that switches itself on only when the `gphoto2` package is already present on the computer. It has
no Windows build (Linux and macOS only, or WSL), so on the owner's Windows laptop this route stays off; the Sony,
Canon and Nikon drivers there speak PTP themselves.

The camera's settings come as text ("5.6", "1/125", "400"); they are translated into Studio's units here.
"""
from __future__ import annotations

import os
import tempfile
import time

from ..ptpip import PTPError
from .ptpstd import (ISO_AUTO, S_APERTURE, S_BATTERY, S_EV, S_ISO, S_SHUTTER, PropInfo, Unsupported, f_to_studio,
                     iso_to_studio, seconds_to_studio, studio_to_seconds)

# gphoto2 setting names, in the order tried (names differ between brands)
NAMES = {S_APERTURE: ("aperture", "f-number"), S_SHUTTER: ("shutterspeed", "shutterspeed2"),
         S_ISO: ("iso", "isospeed"), S_EV: ("exposurecompensation",)}


def available() -> bool:
    try:
        import gphoto2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_shutter(text: str) -> int | None:
    t = text.strip().lower().rstrip("s").replace('"', "").replace(",", ".")
    if t in ("bulb", "b", "time"):
        return 0
    if t in ("auto", ""):
        return None
    try:
        if "/" in t:
            num, den = t.split("/", 1)
            return seconds_to_studio(float(num) / float(den))
        return seconds_to_studio(float(t))
    except ValueError:
        return None


def parse_aperture(text: str) -> int | None:
    t = text.strip().lower().replace("f/", "").replace("f", "")
    try:
        return f_to_studio(float(t))
    except ValueError:
        return None


def parse_iso(text: str) -> int | None:
    t = text.strip().lower()
    if t.startswith("auto"):
        return ISO_AUTO
    try:
        return iso_to_studio(float(t))
    except ValueError:
        return None


def parse_ev(text: str) -> int | None:
    try:
        return int(round(float(text.strip().replace(",", ".")) * 1000))
    except ValueError:
        return None


PARSE = {S_APERTURE: parse_aperture, S_SHUTTER: parse_shutter, S_ISO: parse_iso, S_EV: parse_ev}


class Gphoto2Camera:
    kind = "gphoto2"
    brand = "Camera"
    tested = False

    def __init__(self, host: str = "usb", gp=None):
        if gp is None:
            if not available():
                raise Unsupported("The optional gphoto2 route is not installed on this computer (it is not available "
                                  "on Windows).")
            import gphoto2 as gp  # noqa: N813
        self.gp = gp
        self.cam = None
        self.model = ""
        self._prop_cache: dict[int, PropInfo] = {}
        self._names: dict[int, str] = {}
        self._choices_text: dict[int, list[str]] = {}

    @property
    def name(self) -> str:
        return f"{self.model or 'Camera'} (gphoto2)"

    def connect(self, recover: bool = False) -> None:
        self.cam = self.gp.Camera()
        try:
            self.cam.init()
        except Exception as exc:  # noqa: BLE001
            raise PTPError(f"gphoto2 found no camera: {exc}") from exc
        try:
            self.model = self.cam.get_summary().text.split("Model:", 1)[1].splitlines()[0].strip()
        except Exception:  # noqa: BLE001
            self.model = ""
        self.refresh()

    def close(self) -> None:
        if self.cam is not None:
            try:
                self.cam.exit()
            except Exception:  # noqa: BLE001
                pass
            self.cam = None

    def _cfg(self):
        return self.cam.get_config()

    def refresh(self) -> dict[int, PropInfo]:
        cfg = self._cfg()
        for sid, names in NAMES.items():
            for n in names:
                try:
                    w = cfg.get_child_by_name(n)
                except Exception:  # noqa: BLE001
                    continue
                parse = PARSE[sid]
                cur = parse(str(w.get_value()))
                texts = [str(w.get_choice(i)) for i in range(w.count_choices())] if hasattr(w, "count_choices") else []
                choices, kept = [], []
                for t in texts:
                    v = parse(t)
                    if v is not None and v not in choices:
                        choices.append(v)
                        kept.append(t)
                self._names[sid], self._choices_text[sid] = n, kept
                self._prop_cache[sid] = PropInfo(sid, 6 if sid in (S_SHUTTER, S_ISO) else 4, not w.get_readonly(),
                                                 True, cur if cur is not None else 0, choices=choices)
                break
        try:
            t = str(cfg.get_child_by_name("batterylevel").get_value()).strip().rstrip("%")
            self._prop_cache[S_BATTERY] = PropInfo(S_BATTERY, 3, False, True, int(float(t)), range=(-1, 100, 1))
        except Exception:  # noqa: BLE001
            pass
        return self._prop_cache

    @staticmethod
    def resolve(prop) -> int:
        from .sony import PROPS
        return prop if isinstance(prop, int) else PROPS.get(prop, 0) or int(prop, 16)

    def set(self, prop, value: int) -> None:
        sid = self.resolve(prop)
        if sid not in self._names:
            raise Unsupported("gphoto2 offers no such setting on this camera.")
        p = self._prop_cache[sid]
        if value not in p.choices:
            raise PTPError("The camera does not offer that value right now.")
        text = self._choices_text[sid][p.choices.index(value)]
        cfg = self._cfg()
        cfg.get_child_by_name(self._names[sid]).set_value(text)
        self.cam.set_config(cfg)
        self.refresh()

    def try_set(self, prop, value: int) -> int:
        try:
            self.set(prop, value)
            return 0x2001
        except PTPError:
            return 0x2002

    def live_view(self) -> bytes:
        try:
            f = self.cam.capture_preview()
            return bytes(memoryview(f.get_data_and_size()))
        except Exception as exc:  # noqa: BLE001
            raise PTPError(f"No live picture from gphoto2: {exc}") from exc

    def capture(self, timeout: float = 20.0, af: bool = True) -> list[tuple[int, bytes]]:
        gp = self.gp
        path = self.cam.capture(gp.GP_CAPTURE_IMAGE)
        f = self.cam.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
        data = bytes(memoryview(f.get_data_and_size()))
        ext = os.path.splitext(path.name)[1].lower()
        fmt = 0x3801 if ext in (".jpg", ".jpeg") else 0xB101 if ext == ".arw" else 0xB103 if ext in (".cr2", ".cr3") \
            else 0xB104 if ext == ".nef" else 0x3000
        return [(fmt, data)]

    def movie_record(self, start: bool) -> None:
        cfg = self._cfg()
        try:
            w = cfg.get_child_by_name("movie")
        except Exception as exc:  # noqa: BLE001
            raise Unsupported("gphoto2 cannot start video on this camera.") from exc
        w.set_value(1 if start else 0)
        self.cam.set_config(cfg)

    def fetch_pending(self, *a, **k) -> list:
        return []

    def zoom(self, direction: int) -> None:
        raise Unsupported("No power zoom control through gphoto2.")

    def zoom_position(self):
        return None

    def focus_step(self, step: int) -> None:
        cfg = self._cfg()
        try:
            w = cfg.get_child_by_name("manualfocusdrive")
        except Exception as exc:  # noqa: BLE001
            raise Unsupported("gphoto2 cannot move the focus on this camera.") from exc
        w.set_value(step * 100 if w.get_type() != self.gp.GP_WIDGET_RADIO else
                    (f"Near {min(3, abs(step))}" if step < 0 else f"Far {min(3, abs(step))}"))
        self.cam.set_config(cfg)

    def autofocus(self, hold: float = 0.5) -> None:
        cfg = self._cfg()
        try:
            w = cfg.get_child_by_name("autofocusdrive")
        except Exception as exc:  # noqa: BLE001
            raise Unsupported("gphoto2 cannot start autofocus on this camera.") from exc
        w.set_value(1)
        self.cam.set_config(cfg)
        time.sleep(hold)

    def focus_at(self, xf, yf, max_wait=1.2, lock_in_s1=False) -> dict:
        self.autofocus(max_wait)
        return {"acquired": True, "untested": True, "note": "gphoto2 focuses on the camera's own AF point"}

    def set_af_point(self, xf, yf) -> None:
        raise Unsupported("Tap to focus is not available through gphoto2.")

    def device_info(self) -> dict:
        return {"model": self.model, "manufacturer": "", "route": "gphoto2"}

    def capabilities(self) -> dict:
        return {"live_view": True, "capture": True, "movie": None, "focus": None, "zoom": False,
                "settings": sorted(f"0x{c:04X}" for c in self._prop_cache)}


__all__ = ["Gphoto2Camera", "available", "studio_to_seconds", "tempfile"]
