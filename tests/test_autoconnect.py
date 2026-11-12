"""Automatic connection: the saved route first, then the other one; whichever answers is used and remembered.
His own fixed choice wins. No retry storm when the camera is nowhere."""
import pytest

from camctl import webui
from camctl.drivers.sony import PTPError

IP = "10.0.0.38"


class _Cam:
    def __init__(self, route):
        self.route, self.name = route, "ZV-E10"

    def connect(self, recover=False):
        if self.route not in _World.present:
            raise PTPError(f"no camera on {self.route}")

    def close(self):
        pass


class _World:
    present: set = set()


@pytest.fixture
def world(monkeypatch):
    saved = {"host": IP, "choice": "auto", "mode": "usb"}
    monkeypatch.setattr(webui.wifi, "load", lambda: dict(saved))
    monkeypatch.setattr(webui.wifi, "save", lambda **kw: saved.update(kw) or dict(saved))
    monkeypatch.setattr(webui.wifi, "reachable", lambda host, timeout=0.6: "wifi" in _World.present)
    import camctl.ptpusb as ptpusb
    monkeypatch.setattr(ptpusb, "find_ptp_cameras", lambda vid: ["cam"] if "usb" in _World.present else [])
    monkeypatch.setattr(webui, "_best_live_view_quality", lambda cam: None)
    monkeypatch.setattr(webui.Studio, "rescue_waiting", lambda self, cam, when: [])
    monkeypatch.setattr(webui.Studio, "_new_cam", lambda self: _Cam("usb" if self.host == "usb" else "wifi"))
    _World.present = set()
    return saved


def _studio(host):
    st = webui.Studio(host)
    st.released = True                       # no live-view pump in these tests
    return st


def test_saved_usb_but_camera_on_wifi(world):
    _World.present = {"wifi"}
    st = _studio("usb")
    cam = st.camera()
    assert cam.route == "wifi" and st.host == IP
    assert world["mode"] == "wifi"           # remembered for next time
    assert st.route_at > 0                   # the page says "Connected by Wi-Fi" once


def test_saved_wifi_but_camera_on_usb(world):
    _World.present = {"usb"}
    st = _studio(IP)
    cam = st.camera()
    assert cam.route == "usb" and st.host == "usb" and world["mode"] == "usb"


def test_camera_nowhere_no_retry_storm(world):
    _World.present = set()
    st = _studio("usb")
    with pytest.raises(PTPError):
        st.camera()
    with pytest.raises(webui._Backoff):      # the next try waits (backoff), it does not hammer both routes
        st.camera()
    assert st.host == "usb"
    h = st.health()
    st.released = False
    st.fail_since -= 10                      # past the reconnect grace time
    st._present_at = 0
    h = st.health()
    assert "USB cable and on Wi-Fi" in h["message"]


def test_drop_and_come_back_on_the_other_route(world):
    _World.present = {"usb"}
    st = _studio("usb")
    assert st.camera().route == "usb"
    st.cam = None                            # the cable was pulled; the camera is on Wi-Fi now
    _World.present = {"wifi"}
    assert st.camera().route == "wifi" and st.host == IP


def test_his_fixed_choice_wins(world):
    world["choice"] = "usb"
    _World.present = {"wifi"}
    st = _studio("usb")
    assert st.choice == "usb"
    with pytest.raises(PTPError):
        st.camera()
    assert st.host == "usb"                  # never switched on its own
