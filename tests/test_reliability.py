"""Auto-recovery, backoff, watchdog states and clean shutdown (fake camera, no USB)."""
import time

from camctl import webui
from camctl.ptpip import PTPError


class FlakyCam:
    """Fails to connect `fails` times (like a camera stuck after Studio was killed)."""
    attempts = []
    fails = 0
    closed = 0

    def __init__(self, host):
        self.host = host
        self.client = self
        self.name = "ZV-E10"

    def connect(self, recover=False):
        FlakyCam.attempts.append(recover)
        if len(FlakyCam.attempts) <= FlakyCam.fails:
            raise PTPError("Operation timed out")

    def usb_reset(self):
        pass

    def close(self):
        FlakyCam.closed += 1

    def refresh(self):
        return {}


def test_recovers_with_reset_and_reports_health(monkeypatch):
    monkeypatch.setattr(webui, "SonyCamera", FlakyCam)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    FlakyCam.attempts, FlakyCam.fails = [], 1
    st = webui.Studio("usb")
    st.released = True                     # keep the live-view pump quiet
    assert st.run(lambda c: "ok") == "ok"
    assert FlakyCam.attempts == [False, True]      # plain connect failed, PTP reset worked
    st.released = False
    assert st.health()["state"] == "ok"


def test_backoff_and_watchdog_states(monkeypatch):
    monkeypatch.setattr(webui, "SonyCamera", FlakyCam)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    FlakyCam.attempts, FlakyCam.fails = [], 10 ** 6
    st = webui.Studio("usb")
    st.released = True
    monkeypatch.setattr(st, "_present", lambda: True)
    try:
        st.run(lambda c: "x")
    except PTPError:
        pass
    n = len(FlakyCam.attempts)
    try:
        st.run(lambda c: "x")                         # inside the backoff window: no new USB attempts
    except PTPError:
        pass
    assert len(FlakyCam.attempts) == n
    st.released = False
    assert st.health()["state"] == "recovering"
    st.fail_since = time.time() - 10
    h = st.health()
    assert h["state"] == "unresponsive" and "off and on" in h["message"]
    monkeypatch.setattr(st, "_present", lambda: False)
    assert st.health()["state"] == "absent"
    # camera comes back: the next attempt after the backoff reconnects and clears the alarm
    FlakyCam.fails = 0
    st._retry_at = 0
    assert st.run(lambda c: "back") == "back" and st.health()["state"] == "ok"


def test_shutdown_closes_session(monkeypatch):
    monkeypatch.setattr(webui, "SonyCamera", FlakyCam)
    FlakyCam.attempts, FlakyCam.fails, FlakyCam.closed = [], 0, 0
    st = webui.Studio("usb")
    st.released = True
    st.run(lambda c: None)
    st.shutdown()
    assert FlakyCam.closed == 1 and st.cam is None and st.health()["state"] == "released"


def test_waiting_image_is_fetched_not_dropped_on_close_and_connect(monkeypatch, tmp_path):
    """A body-button shot waiting for the PC is transferred before the session closes (no silent loss)."""
    import io as _io
    from PIL import Image as _I
    from camctl import webui as w
    from camctl.drivers.sony import PropInfo
    monkeypatch.setattr(w, "CAPTURE_DIR", tmp_path)
    b = _io.BytesIO(); _I.new("RGB", (8, 8)).save(b, "JPEG"); jpg = b.getvalue()

    class Cam:
        name = "fake"
        closed = False
        def __init__(self): self.pending = 0x8001
        def refresh(self): return {0xD215: PropInfo(0xD215, 4, False, True, self.pending)}
        def fetch_pending(self, timeout=10, **kw):
            self.pending = 0
            return [(0x3801, jpg)]
        def close(self): self.closed = True

    st = w.Studio("usb")
    st.cam = cam = Cam()
    st.shutdown()
    assert cam.closed and len(list(tmp_path.glob("*.jpg"))) == 1
    assert "Saved 1 waiting image" in st.waiting_note
    cam2 = Cam()
    assert st.rescue_waiting(cam2, "on connect") and len(list(tmp_path.glob("*.jpg"))) == 2
    cam3 = Cam(); cam3.pending = 0
    assert st.rescue_waiting(cam3, "on connect") == []
