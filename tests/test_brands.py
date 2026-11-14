"""Canon, Nikon, plain-PTP and gphoto2 drivers against SIMULATED cameras (tests/fake_ptp_brands.py), the per-camera
data and the driver choice. None of this was run on a real Canon or Nikon: these tests prove the drivers are
consistent with the codes they were written from, nothing more."""
import struct

import pytest

from camctl.drivers import base, registry
from camctl.drivers.canon import CanonEOSCamera, aperture_from_canon, ev_from_canon, iso_from_canon, shutter_from_canon
from camctl.drivers.nikon import NikonCamera
from camctl.drivers.ptpstd import PTPCamera, Unsupported, seconds_to_studio
from camctl.drivers.sony import PROPS, format_value
from fake_ptp_brands import FakeCanon, FakeNikon, FakePlainPTP

AP, SH, ISO, WB, EV, FM = (PROPS[k] for k in ("aperture", "shutter_speed", "iso", "white_balance",
                                              "exposure_compensation", "focus_mode"))


# -- value conversion ---------------------------------------------------------------------------------------------
def test_canon_codes_become_the_printed_values():
    assert [format_value(AP, aperture_from_canon(c)) for c in (0x08, 0x20, 0x23, 0x25, 0x28, 0x30, 0x38, 0x50)] == \
        ["f/1", "f/2.8", "f/3.2", "f/3.5", "f/4", "f/5.6", "f/8", "f/22"]
    assert [format_value(SH, shutter_from_canon(c)) for c in (0x10, 0x38, 0x60, 0x65, 0x70, 0x98, 0xA0)] == \
        ["30s", "1s", "1/30s", "1/50s", "1/125s", "1/4000s", "1/8000s"]
    assert shutter_from_canon(0x0C) == 0                                      # bulb
    assert [iso_from_canon(c) for c in (0x48, 0x4B, 0x58, 0x68, 0x78)] == [100, 125, 400, 1600, 6400]
    assert iso_from_canon(0) == 0xFFFFFF                                       # auto
    assert [ev_from_canon(c) for c in (0x00, 0x03, 0x05, 0x08, 0xFD, 0xF8)] == [0, 333, 667, 1000, -333, -1000]


def test_standard_exposure_time_is_snapped():
    assert format_value(SH, seconds_to_studio(1 / 124.9)) == "1/125s"
    assert format_value(SH, seconds_to_studio(1.3)) == "13/10s"
    assert seconds_to_studio(0) == 0


# -- Canon ----------------------------------------------------------------------------------------------------------
def _canon():
    fake = FakeCanon()
    cam = CanonEOSCamera(client=fake)
    cam.connect()
    return cam, fake


def test_canon_connects_in_remote_mode_and_sends_files_to_the_pc():
    cam, fake = _canon()
    assert fake.remote and fake.values[0xD11C] == 4                           # capture destination: PC
    assert cam.name == "Canon EOS R50 (simulated)"
    p = cam.refresh()
    assert p[AP].value == 560 and p[SH].value == (1 << 16) | 125 and p[ISO].value == 400
    assert 280 in p[AP].choices and (1 << 16) | 50 in p[SH].choices and 0xFFFFFF in p[ISO].choices
    assert p[WB].value == 2 and 32785 in p[WB].choices                         # Canon Auto -> Studio auto, shade
    assert p[FM].writable is False                                            # the lens switch decides on a Canon


def test_canon_set_writes_the_canon_code_and_reads_it_back():
    cam, fake = _canon()
    cam.set("aperture", 800)
    assert fake.values[0xD101] == 0x38 and cam.refresh()[AP].value == 800
    cam.set("shutter_speed", (1 << 16) | 50)
    assert fake.values[0xD102] == 0x65
    cam.set("iso", 1600)
    assert fake.values[0xD103] == 0x68
    cam.set("exposure_compensation", -333)
    assert fake.values[0xD104] == 0xFD
    with pytest.raises(Exception):
        cam.set("aperture", 120)                                              # f/1.2 is not on this lens
    with pytest.raises(Unsupported):
        cam.set("focus_mode", 1)


def test_canon_live_view_waits_for_the_first_frame():
    cam, fake = _canon()
    j = cam.live_view()
    assert j[:2] == b"\xff\xd8" and fake.values[0xD1B1] == 1 and fake.values[0xD1B0] & 2


def test_canon_photo_returns_raw_and_jpeg():
    cam, fake = _canon()
    files = cam.capture(timeout=5)
    assert sorted(f for f, _ in files) == [0x3801, 0xB108]
    ops = [op for op, *_ in fake.log]
    assert ops.index(0x9128) < ops.index(0x9129) and 0x9117 in ops            # press, release, transfer complete


def test_canon_movie_and_focus():
    cam, fake = _canon()
    cam.movie_record(True)
    assert fake.values[0xD1B8] == 4
    cam.movie_record(False)
    assert fake.values[0xD1B8] == 0
    cam.focus_step(-2)
    cam.focus_step(9)
    drives = [p[0] for op, p, _ in fake.log if op == 0x9155]
    assert drives == [0x0001, 0x8003]                                         # near small, far large


def test_a_canon_without_remote_mode_is_refused_plainly():
    fake = FakeCanon()
    fake.OPS = [0x1001, 0x1002, 0x1003]
    with pytest.raises(Unsupported, match="not an EOS"):
        CanonEOSCamera(client=fake).connect()


# -- Nikon ----------------------------------------------------------------------------------------------------------
def _nikon():
    fake = FakeNikon()
    cam = NikonCamera(client=fake)
    cam.connect()
    return cam, fake


def test_nikon_reads_standard_and_nikon_properties():
    cam, _ = _nikon()
    p = cam.refresh()
    assert p[AP].value == 560 and p[ISO].value == 400 and p[SH].value == (1 << 16) | 125
    assert (13 << 16) | 10 in p[SH].choices and 0 in p[SH].choices          # 1.3 s and bulb from 0xD100
    assert p[FM].value == 2 and 32772 in p[FM].choices                       # AF-S, AF-C in Studio's numbering
    assert p[0xD218].value == 80                                             # battery in percent


def test_nikon_set_and_shoot():
    cam, fake = _nikon()
    cam.set("aperture", 800)
    cam.set("shutter_speed", (1 << 16) | 60)
    cam.set("focus_mode", 32772)
    assert fake.p[0x5007][2] == 800 and fake.p[0xD100][2] == (1 << 16) | 60 and fake.p[0x500A][2] == 0x8011
    files = cam.capture(timeout=5)
    assert [f for f, _ in files] == [0x3801] and files[0][1][:2] == b"\xff\xd8"


def test_nikon_live_view_skips_the_header_and_knows_the_size():
    cam, fake = _nikon()
    j = cam.live_view()
    assert j[:2] == b"\xff\xd8" and cam.lv_size == (640, 424)
    cam.movie_record(True)
    assert fake.recording
    cam.movie_record(False)
    cam.focus_at(0.5, 0.5)
    assert any(op == 0x9205 and p == (320, 212) for op, p, _ in fake.log)


# -- plain PTP ------------------------------------------------------------------------------------------------------
def test_plain_ptp_camera_gives_settings_and_a_shutter_only():
    fake = FakePlainPTP()
    cam = PTPCamera(client=fake)
    cam.brand = "Fujifilm"
    cam.connect()
    p = cam.refresh()
    assert set(p) >= {AP, SH, ISO} and p[SH].value == seconds_to_studio(80 / 10000)
    files = cam.capture(timeout=5)
    assert len(files) == 1
    with pytest.raises(Unsupported, match="no live picture"):
        cam.live_view()
    with pytest.raises(Unsupported):
        cam.movie_record(True)


# -- per-camera data, guide, driver choice ---------------------------------------------------------------------------
def test_camera_data_is_honest():
    d = registry.data()
    assert d["models"] == [m for m in d["models"] if m["status"] == "tested"]  # only real, tested models are listed
    assert all("untested" in v["status"] or v.get("driver") in (None, "sony") for v in d["brands"].values())
    assert d["routes"]["sdk"]["bundled"] is False and d["routes"]["gphoto2"]["bundled"] is False
    assert registry.driver_name("GoPro") is None and registry.driver_name("Canon") == "canon"


def test_guide_for_new_brands_says_untested_and_never_installs():
    g = base.guide("Canon", None, connected=False)
    assert g["kind"] == "untested" and "untested" in g["title"] and "never installs" in " ".join(g["steps"])
    g = base.guide("Nikon", "Z 50", connected=True)
    assert g["kind"] == "untested" and "Check this camera" in g["text"]
    g = base.guide("Fujifilm", None, connected=False)
    assert "no live picture" in g["text"]
    assert base.guide("GoPro", None, connected=False)["kind"] == "unsupported"


def test_driver_choice_keeps_sony_on_the_sony_driver(monkeypatch):
    from camctl.drivers.sony import SonyCamera
    monkeypatch.setattr(registry, "usb_cameras", lambda: [(0x04A9, 0x32F1), (0x054C, 0x0D97)])
    assert isinstance(registry.make_usb_driver(), SonyCamera)
    monkeypatch.setattr(registry, "usb_cameras", lambda: [])
    assert isinstance(registry.make_usb_driver(), SonyCamera)              # nothing found: the old messages
    monkeypatch.setattr(registry, "usb_cameras", lambda: [(0x04A9, 0x32F1)])
    assert isinstance(registry.make_usb_driver(), CanonEOSCamera)
    monkeypatch.setattr(registry, "usb_cameras", lambda: [(0x04B0, 0x0442)])
    assert isinstance(registry.make_usb_driver(), NikonCamera)
    monkeypatch.setattr(registry, "usb_cameras", lambda: [(0x04CB, 0x02E3)])
    drv = registry.make_usb_driver()
    assert type(drv) is PTPCamera and drv.brand == "Fujifilm"


def test_an_sdk_adapter_registered_on_the_users_computer_wins(monkeypatch):
    made = []
    monkeypatch.setattr(registry, "SDK_ADAPTERS", {})
    registry.register_sdk("Canon", lambda host: made.append(host) or "sdk-driver")
    monkeypatch.setattr(registry, "usb_cameras", lambda: [(0x04A9, 0x32F1)])
    assert registry.make_usb_driver() == "sdk-driver" and made == ["usb"]


def test_gphoto2_route_is_off_unless_installed(monkeypatch):
    from camctl.drivers import gphoto
    monkeypatch.setattr(gphoto, "available", lambda: False)
    monkeypatch.setattr(registry, "usb_cameras", lambda: [])
    from camctl.drivers.sony import SonyCamera
    assert isinstance(registry.make_usb_driver(prefer="gphoto2"), SonyCamera)
    assert gphoto.parse_shutter("1/125") == (1 << 16) | 125 and gphoto.parse_shutter("2.5") == (25 << 16) | 10
    assert gphoto.parse_aperture("f/5.6") == 560 and gphoto.parse_iso("Auto ISO") == 0xFFFFFF


def test_every_driver_offers_what_studio_needs():
    from camctl.drivers.gphoto import Gphoto2Camera
    for cls in (CanonEOSCamera, NikonCamera, PTPCamera, Gphoto2Camera):
        for fn in ("connect", "close", "refresh", "set", "live_view", "capture", "movie_record", "zoom", "focus_step",
                   "focus_at", "fetch_pending", "try_set"):
            assert callable(getattr(cls, fn, None)), (cls.__name__, fn)


def test_gphoto2_driver_with_a_stand_in_library():
    """The gphoto2 driver's translation, with a tiny stand-in for the gphoto2 package (no camera, no install)."""
    from camctl.drivers.gphoto import Gphoto2Camera

    class W:
        def __init__(self, v, ch):
            self.v, self.ch = v, ch

        def get_value(self):
            return self.v

        def set_value(self, v):
            self.v = v

        def count_choices(self):
            return len(self.ch)

        def get_choice(self, i):
            return self.ch[i]

        def get_readonly(self):
            return 0

    class Cfg:
        def __init__(self, ws):
            self.ws = ws

        def get_child_by_name(self, n):
            return self.ws[n]

    ws = {"aperture": W("5.6", ["4", "5.6", "8"]), "shutterspeed": W("1/125", ["1/60", "1/125", "bulb"]),
          "iso": W("400", ["Auto", "100", "400"]), "batterylevel": W("75%", [])}

    class Cam:
        def get_config(self):
            return Cfg(ws)

        def set_config(self, c):
            pass

    g = Gphoto2Camera(gp=object())
    g.cam = Cam()
    p = g.refresh()
    assert p[AP].value == 560 and p[SH].choices == [(1 << 16) | 60, (1 << 16) | 125, 0] and p[ISO].choices[0] == 0xFFFFFF
    g.set("aperture", 800)
    assert ws["aperture"].v == "8"
    assert p[0xD218].value == 75


def test_packed_struct_sanity():
    assert struct.calcsize("<HIH") == 8


def test_raw_files_get_their_makers_ending():
    from camctl.drivers.base import file_ext
    tiff = b"II*\x00" + b"\x00" * 20
    assert file_ext(tiff) == ".arw" and file_ext(tiff, "Sony") == ".arw"       # unchanged for Sony
    assert file_ext(tiff, "Canon") == ".cr2" and file_ext(tiff, "Nikon") == ".nef"
    assert file_ext(b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 8, "Canon") == ".cr3"
    assert file_ext(b"FUJIFILMCCD-RAW 0201") == ".raf" and file_ext(b"\xff\xd8\xff\xe0") == ".jpg"
