"""Wi-Fi routes for the other makers (Canon / Nikon PTP/IP, Fujifilm PC Shoot, Panasonic, OM System, Ricoh / Pentax).

UNTESTED ON REAL CAMERAS: these run Studio's real drivers against the simulated cameras in fake_wifi_brands.py.
"""
import struct

import pytest
from starlette.testclient import TestClient

from camctl import webui, wifi
from camctl.drivers import registry
from camctl.drivers.fuji import FujiCamera, FujiPTPIPClient, init_request
from camctl.drivers.lumix import LumixCamera, apex_aperture, apex_shutter
from camctl.drivers.olympus import OlympusCamera, oly_shutter, rtp_payload
from camctl.drivers.ptpstd import PTPCamera, seconds_to_studio
from camctl.drivers.ricoh import RicohCamera, ricoh_shutter
from camctl.drivers.sony import PROPS, SonyCamera
from camctl.ptpip import PTPError, PTPIPClient

from fake_wifi_brands import FakeFuji, FakeLumix, FakeOlympus, FakeRicoh

FAKES = {"Panasonic": FakeLumix, "OM System / Olympus": FakeOlympus, "Ricoh / Pentax": FakeRicoh, "Fujifilm": FakeFuji}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi, "BASE", tmp_path)
    monkeypatch.setattr(wifi, "CONFIG", tmp_path / "wifi.json")
    return tmp_path


# -- which driver, which port ----------------------------------------------------------------------------------------
def test_each_maker_gets_its_wifi_driver_and_port():
    want = {"Sony": (SonyCamera, 15740), "Canon": (PTPCamera, 15740), "Nikon": (PTPCamera, 15740),
            "Fujifilm": (FujiCamera, 55740), "Panasonic": (LumixCamera, 80),
            "OM System / Olympus": (OlympusCamera, 80), "Ricoh / Pentax": (RicohCamera, 80)}
    assert set(registry.wifi_brands()) == set(want)                      # Leica stays USB only
    for brand, (cls, port) in want.items():
        d = registry.make_wifi_driver(brand, "10.0.0.9", guid=b"\1" * 16, name="t")
        assert isinstance(d, cls), brand
        client = getattr(d, "client", None)
        got = client.port if client is not None else d.port
        assert got == port, brand
    assert isinstance(registry.make_wifi_driver(None, "10.0.0.9"), SonyCamera)          # nothing saved: Sony, as before
    canon = registry.make_wifi_driver("Canon", "10.0.0.9:15741", guid=b"\2" * 16)
    assert isinstance(canon.client, PTPIPClient) and canon.client.port == 15741 and canon.brand == "Canon"


# -- value conversions -----------------------------------------------------------------------------------------------
def test_camera_strings_turn_into_studio_values():
    assert apex_shutter("2560/256") == seconds_to_studio(1 / 1024) == seconds_to_studio(1 / 1000)
    assert apex_aperture("1280/256") == 560
    assert ricoh_shutter("1.250") == seconds_to_studio(1 / 250) and ricoh_shutter("2.1") == seconds_to_studio(2)
    assert oly_shutter("250") == seconds_to_studio(1 / 250) and oly_shutter('2"') == seconds_to_studio(2)
    assert oly_shutter("Bulb") == 0 and ricoh_shutter("bulb") == 0
    pkt = bytes([0x80, 0xE0]) + struct.pack(">HI", 1, 77) + b"\0\0\0\1" + b"abc"
    assert rtp_payload(pkt) == (b"abc", 77, True)
    p = init_request(b"\x42" * 16, "camctl")
    assert len(p) == 82 and struct.unpack_from("<III", p) == (82, 1, 0x8F53E4F2) and p[12:28] == b"\x42" * 16


# -- every simulated maker: connect, settings, live picture, shutter ------------------------------------------------
@pytest.mark.parametrize("brand", list(FAKES))
def test_driver_against_simulated_camera(brand):
    fake = FAKES[brand]()
    cam = registry.make_wifi_driver(brand, fake.host, guid=b"\1" * 16, name="camctl test")
    try:
        cam.connect()
        assert brand.split()[0].lower() in cam.name.lower()
        props = cam.refresh()
        ap = props[PROPS["aperture"]]
        assert ap.value == 560 and ap.writable
        target = next(v for v in ap.choices if v != ap.value)
        cam.set("aperture", target)
        assert cam.refresh()[PROPS["aperture"]].value == target
        jpg = cam.live_view()
        assert jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"
        files = cam.capture(timeout=6)
        assert fake.shots == 1
        if brand == "Panasonic":
            assert files == [] and cam.kept_on_card                       # the photo stays on its card
        else:
            assert files and files[0][1][:2] == b"\xff\xd8"
        if brand in ("OM System / Olympus", "Ricoh / Pentax"):
            assert {f for f, _ in files} == {0x3801, 0x3000}               # JPEG and RAW of the same shot
    finally:
        cam.close()
        fake.close()


def test_lumix_waits_for_the_ok_on_the_camera(monkeypatch):
    import camctl.drivers.lumix as L
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    fake = FakeLumix(ask_first=3)
    cam = LumixCamera("127.0.0.1", port=fake.port)
    try:
        cam.connect()
        assert cam.model == "DC-GH5" and fake.mode == "rec"
        cam.movie_record(True)
        assert fake.rec and cam.refresh() and cam.recording
        cam.movie_record(False)
        assert not fake.rec
        cam.zoom(1)
        assert ("GET", "/cam.cgi", "mode=camcmd&value=tele-normal") in fake.requests
    finally:
        cam.close()
        fake.close()
    assert fake.mode == "play"                                            # left in play mode, as the app does


def test_olympus_sends_its_app_headers_and_never_records():
    fake = FakeOlympus()
    cam = OlympusCamera("127.0.0.1", port=fake.port)
    try:
        cam.connect()
        assert all(h.get("User-Agent") == "OI.Share v2" for h in fake.headers_seen)
        assert cam.focus_at(0.5, 0.5)["ok"]
        with pytest.raises(PTPError):
            cam.movie_record(True)
    finally:
        cam.close()
        fake.close()


def test_fuji_start_sequence_and_refusal():
    fake = FakeFuji()
    cam = FujiCamera(fake.host, name="camctl test", guid=b"\3" * 16)
    try:
        cam.connect()
        assert isinstance(cam.client, FujiPTPIPClient) and cam.client.camera_name == "X-T4"
        assert fake.props[0xDF01] == 5 and 0x101C in fake.ops and fake.capture_open.is_set()
        assert cam.info["model"] == "X-T4"                                # empty DeviceInfo over Wi-Fi: filled in
        cam.movie_record(True)
        assert fake.recording
        cam.movie_record(False)
    finally:
        cam.close()
        fake.close()
    bad = FakeFuji(refuse=True)
    try:
        with pytest.raises(PTPError, match="Pair it"):
            FujiCamera(bad.host).connect()
    finally:
        bad.close()


# -- finding cameras on the network ----------------------------------------------------------------------------------
def test_access_point_cameras_are_recognised(monkeypatch):
    fake = FakeLumix()
    try:
        assert wifi._http_probe(f"127.0.0.1:{fake.port}", "cam.cgi?mode=getstate", "<camrply")
        assert not wifi._http_probe(f"127.0.0.1:{fake.port}", "v1/props", '"model"')
    finally:
        fake.close()
    seen = []
    monkeypatch.setattr(wifi, "_http_probe", lambda ip, path, word: seen.append(ip) or ip == "192.168.54.1")
    hits = wifi.access_point_cameras(["192.168.54", "10.0.0"])
    assert seen == ["192.168.54.1"]                                       # only the networks this PC is on
    assert hits == [{"ip": "192.168.54.1", "host": "192.168.54.1:80", "name": "", "model": "", "udn": "",
                     "via": "camera network", "brand": "Panasonic", "usable": True}]


def test_discover_says_the_maker(monkeypatch, cfg):
    monkeypatch.setattr(wifi, "ssdp", lambda: [
        {"ip": "10.0.0.5", "name": "EOS R6", "model": "", "udn": "", "manufacturer": "Canon", "via": "ssdp", "brand": "Canon"},
        {"ip": "10.0.0.6", "name": "ZV-E10", "model": "", "udn": "", "manufacturer": "Sony", "via": "ssdp", "brand": "Sony"},
        {"ip": "10.0.0.7", "name": "GH5", "model": "", "udn": "", "manufacturer": "Panasonic", "via": "ssdp", "brand": "Panasonic"}])
    monkeypatch.setattr(wifi, "access_point_cameras", lambda: [])
    monkeypatch.setattr(wifi, "_port_open", lambda ip, port, t: (ip, port) in {("10.0.0.5", 15740), ("10.0.0.6", 15740)})
    monkeypatch.setattr(wifi, "scan", lambda port=wifi.PTPIP_PORT, skip=None, **k:
                        ["10.0.0.20"] if port == wifi.PTPIP_PORT else ["10.0.0.30"])
    wifi.remember_brand("10.0.0.20", "Nikon")
    by = {c["ip"]: c for c in wifi.discover()}
    assert by["10.0.0.5"]["brand"] == "Canon" and by["10.0.0.5"]["usable"] and by["10.0.0.5"]["host"] == "10.0.0.5"
    assert by["10.0.0.6"]["brand"] == "Sony" and by["10.0.0.6"]["ptpip"]
    assert by["10.0.0.7"]["host"] == "10.0.0.7:80" and by["10.0.0.7"]["usable"]
    assert by["10.0.0.20"]["brand"] == "Nikon"                            # port scan: the maker he picked before
    assert by["10.0.0.30"]["brand"] == "Fujifilm" and by["10.0.0.30"]["host"] == "10.0.0.30:55740"


# -- Studio: switching to another maker's Wi-Fi camera ---------------------------------------------------------------
def test_studio_connects_by_maker_and_sony_stays_as_before(cfg):
    st = webui.Studio("usb")
    st.released = True
    st.set_connection("wifi", "10.0.0.38")
    assert wifi.brand_for(st.host) is None and isinstance(st._new_cam(), SonyCamera)
    st.set_connection("wifi", "10.0.0.40", brand="Fujifilm")
    assert st.host == "10.0.0.40:55740" and isinstance(st._new_cam(), FujiCamera)
    st.set_connection("wifi", "10.0.0.40:55740")                          # no maker sent: the saved one is kept
    assert isinstance(st._new_cam(), FujiCamera)
    st.set_connection("wifi", "10.0.0.38", brand="Sony")
    assert isinstance(st._new_cam(), SonyCamera) and wifi.load()["brands"] == {"10.0.0.40": "Fujifilm"}
    with pytest.raises(ValueError):
        st.set_connection("wifi", "10.0.0.41", brand="Leica")
    st.shutdown()


def test_studio_live_picture_and_photo_on_card_from_a_lumix(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    fake = FakeLumix()
    st = webui.Studio("usb")
    st.released = True
    try:
        st.set_connection("wifi", f"127.0.0.1:{fake.port}", brand="Panasonic")
        frame, _ = st.latest_frame(-1, timeout=8)
        assert frame[:2] == b"\xff\xd8" and isinstance(st.cam, LumixCamera)
        app = webui.build_app(st, start_auto=False)
        with TestClient(app) as c:
            conn = c.get("/api/connection").json()
            assert conn["brand"] == "Panasonic" and "Panasonic" in conn["wifi_brands"]
            assert conn["wifi_brands"]["Panasonic"]["setup"] and not conn["wifi_brands"]["Panasonic"]["tested"]
            r = c.post("/api/shoot").json()
        assert r.get("on_card") and "card" in r["note"] and not r.get("failed") and fake.shots == 1
    finally:
        st.shutdown()
        fake.close()
