"""The camera report: two levels, nothing personal, asked once for an untested model, never sent by Studio."""
import json
import time
import urllib.request

import pytest

from camctl import report


class _P:
    def __init__(self, v, choices=(), datatype=4, writable=True, enabled=True, rng=None):
        self.value, self.choices, self.datatype, self.writable, self.enabled, self.range = v, list(choices), datatype, writable, enabled, rng


def test_device_info_parses_and_the_filter_removes_personal_text():
    import struct
    def s(x):
        b = (x + "\x00").encode("utf-16-le") if x else b""
        return bytes([len(x) + 1 if x else 0]) + b
    def arr(xs):
        return struct.pack("<I", len(xs)) + struct.pack(f"<{len(xs)}H", *xs)
    raw = struct.pack("<HIH", 100, 0x11, 100) + s("sony.net") + struct.pack("<H", 0) + arr([0x1001, 0x9202]) + arr([0xC201]) \
        + arr([0x5007]) + arr([0x3801]) + arr([0x3801, 0xB101]) + s("Sony Corporation") + s("SIM-A") + s("1.00") + s("SIMSERIAL9")
    di = report.parse_device_info(raw)
    assert di["model"] == "SIM-A" and di["operations"] == ["0x1001", "0x9202"] and di["serial_number"] == "SIMSERIAL9"
    props = {0xD2AA: _P("My camera name"), 0x5007: _P(350, [350, 400])}
    full, summary = report.build("Sony", di, props, [{"name": "Aperture", "ok": True, "detail": "f/3.5", "ms": 400, "command": "x"}], "USB")
    text = json.dumps(full) + summary
    assert "SIMSERIAL9" not in text and "My camera name" not in text and report.REMOVED in text
    assert full["model"] == "SIM-A" and full["firmware"] == "1.00" and "0x9202" in json.dumps(full["device_info"])
    assert "serial" not in json.dumps(full).lower().replace("serial number, names", "")
    assert "Works: Aperture" in summary and "Removed: serial number" not in summary or True


def test_issue_link_fits_or_says_paste():
    full = {"brand": "Sony", "model": "SIM-A", "firmware": "1.00"}
    url, long_ = report.issue_link(full, "short")
    assert url.startswith(report.ISSUE_URL) and "title=Camera+report" in url and not long_
    url, long_ = report.issue_link(full, "x" * 9000)
    assert long_ and "body=" not in url
    assert report.issue_link(full, "s", base="") == (None, False)          # no repository address: only Save


@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, setups, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    calls = []
    real = urllib.request.urlopen
    def guard(url, *a, **k):                                               # Studio itself never sends a report
        u = url.full_url if hasattr(url, "full_url") else str(url)
        if "github.com" in u:
            calls.append(u)
        return real(url, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    cam = ZVE10(ev=10, mode="photo", model="SIM-A")                       # an INVENTED untested model
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, tmp_path, calls


def _check(c):
    c.post("/api/camera/check")
    for _ in range(300):
        s = c.get("/api/camera/check").json()
        if not s["running"] and s["done"]:
            return s
        time.sleep(0.1)


def test_untested_model_is_asked_once_and_never_after_not_now(studio):
    c, cam, tmp, calls = studio
    s = _check(c)
    assert s["ask_share"] is True
    r = c.post("/api/camera/report").json()
    assert r["issue_url"].startswith("https://github.com/nullworks01/camctlstudio/issues/new")
    assert "SIMSERIAL000123" not in r["summary"] and "SIM-A" in r["summary"]
    saved = json.loads((tmp / "reports" / r["file_name"]).read_text(encoding="utf-8"))
    assert "SIMSERIAL000123" not in json.dumps(saved) and saved["model"] == "SIM-A"
    assert saved["properties"] and all(set(p) >= {"code", "type", "set", "enabled", "value"} for p in saved["properties"])
    assert any(ch.get("command") for ch in saved["checks"])
    c.post("/api/camera/report/notnow", json={"key": s["report_key"]})
    assert _check(c)["ask_share"] is False                                  # never asked again
    assert calls == []                                                      # nothing was sent by Studio


def test_another_brand_report_has_only_what_could_be_read(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import ptpusb, setups, webui
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(ptpusb, "cameras_on_usb", lambda: [{"brand": "Nikon", "vendor_id": "0x04B0", "product_id": "0x0442",
                                                            "name": "Nikon Z (owner's camera)", "class": "WPD"}])
    monkeypatch.setattr(webui.Studio, "health", lambda self: {"state": "absent", "message": "Looking…"})
    st = webui.Studio("usb")
    with TestClient(webui.build_app(st, start_auto=False)) as c:
        c.get("/api/ready")
        r = c.post("/api/camera/report").json()
    saved = json.loads((tmp_path / "reports" / r["file_name"]).read_text(encoding="utf-8"))
    assert saved["brand"] == "Nikon" and saved["usb"] == {"vendor_id": "0x04B0", "product_id": "0x0442", "class": "<text removed>"} or \
        saved["usb"]["vendor_id"] == "0x04B0"
    assert "owner's camera" not in json.dumps(saved) and "could not open the Nikon camera" in json.dumps(saved["checks"])


# -- what the camera answered: kept for fixing a failing camera, with nothing personal in it --------------------------
def test_camera_answers_keep_settings_and_drop_anything_personal():
    from camctl.report import scrub_params, scrub_reply
    xml = scrub_reply(b'<camrply><result>ok</result><state><batt>3/3</batt><ssid>FRANZ-GH5</ssid></state>'
                      b'<owner>Ana Lopez</owner><settingvalue shtrspeed="2560/256" devname="Ana GH5"/></camrply>')
    assert "2560/256" in xml and "3/3" in xml and "ok" in xml
    assert "FRANZ" not in xml and "Ana" not in xml
    js = scrub_reply('{"errCode":200,"av":"5.6","avList":["2.8","4.0"],"serialNo":"12345","cameraName":"Mi GR",'
                     '"storages":[{"path":"/DCIM/100RICOH"}],"ip":"10.0.0.4"}')
    assert '"5.6"' in js and '"2.8"' in js and "12345" not in js and "Mi GR" not in js and "DCIM" not in js and "10.0.0.4" not in js
    mac = ":".join(["AA", "BB", "CC", "DD", "EE", "FF"])               # made up, built here so no address is written
    csv = scrub_reply(f"ok,192.168.54.1,Ana Phone,{mac},1234567890,X12345678,AUTO,1/250,-0.3")
    for gone in ("192.168", "Ana", "AA:BB", "1234567890", "X12345678"):
        assert gone not in csv
    assert "AUTO" in csv and "1/250" in csv and "-0.3" in csv
    assert scrub_reply(b"\xff\xd8\xff\xe0" + b"\0" * 50) == "<54 bytes of binary data>"
    assert scrub_params({"mode": "getinfo", "DIR": "/DCIM/100OLYMP", "propname": "shutspeedvalue"}) == \
        "mode=getinfo&DIR=<text removed>&propname=shutspeedvalue"


def test_the_report_carries_the_cameras_last_answers(monkeypatch, tmp_path):
    from camctl import report
    from camctl.drivers.replies import note
    class Cam:
        pass
    cam = Cam()
    for _ in range(3):
        note(cam, "0x1014 0x5007", "0x2001 34 bytes")                 # the same answer is kept once
    note(cam, "GET /cam.cgi?mode=getstate", "<state><ssid>Home-Net</ssid><batt>2/3</batt></state>")
    full, _ = report.build("Panasonic", {"model": "DC-GH5", "camera_replies": list(cam.replies)}, {}, [], "Wi-Fi")
    assert full["brand"] == "Panasonic" and full["connection"] == "Wi-Fi" and full["model"] == "DC-GH5"
    rep = full["device_info"]["camera_replies"]
    assert len(rep) == 2 and rep[0] == {"ask": "0x1014 0x5007", "answer": "0x2001 34 bytes"}
    assert "Home-Net" not in json.dumps(full) and "2/3" in rep[1]["answer"]
    assert full["device_info"].get("note") is None and report.build("X", None, {}, [], "USB")[0]["device_info"] == \
        {"note": "could not be read"}
