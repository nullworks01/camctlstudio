"""The camera guide on connect: tested model, untested Sony, another brand (never fake support)."""
from camctl.drivers import base


def test_tested_model_has_its_steps_only_when_something_is_not_right():
    g = base.guide("Sony", "ZV-E10", connected=False)
    assert g["kind"] == "tested" and any("PC Remote" in s for s in g["steps"])
    assert base.guide("Sony", "ZV-E10", connected=True)["steps"] == []


def test_untested_sony_points_to_check_this_camera():
    g = base.guide("Sony", "SIM-A", connected=True)
    assert g["kind"] == "untested" and "Check this camera" in g["text"]


def test_another_brand_is_said_plainly_and_not_faked():
    g = base.guide("GoPro", None, connected=False)
    assert g["kind"] == "unsupported" and "not supported yet" in g["title"] and "report" in g["text"]
    g = base.guide("Canon", None, connected=False)                       # a driver exists, but never run on a Canon
    assert g["kind"] == "untested" and "has not been tested" in g["text"]
    assert base.brand_of(0x04A9) == "Canon" and base.brand_of(0x1234) is None


def test_the_sony_driver_offers_what_studio_needs():
    from camctl.drivers.sony import SonyCamera
    for fn in ("connect", "close", "refresh", "set", "live_view", "capture", "movie_record", "zoom", "focus_step", "focus_at"):
        assert callable(getattr(SonyCamera, fn, None)), fn


def test_ready_says_another_brand_plainly(monkeypatch):
    import time
    from starlette.testclient import TestClient
    from camctl import ptpusb, webui
    monkeypatch.setattr(ptpusb, "cameras_on_usb", lambda: [{"brand": "Nikon", "vendor_id": "0x04B0", "product_id": "0x0442",
                                                            "name": "Nikon camera", "class": "WPD"}])
    monkeypatch.setattr(webui.Studio, "health", lambda self: {"state": "absent", "message": "Looking for the camera…"})
    st = webui.Studio("usb")
    with TestClient(webui.build_app(st, start_auto=False)) as c:
        r = c.get("/api/ready").json()
    assert r["show"] and r["camera"]["guide"]["kind"] == "untested"
    assert r["camera"]["text"].startswith("Studio can control Nikon cameras over the USB cable")
    assert "never installs drivers" in r["camera"]["text"]
