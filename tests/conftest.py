"""Stop every test's Studio live-view pump afterwards, so background threads don't pile up
(each one keeps pulling frames from its fake camera and slows the whole suite down)."""
import pytest

from camctl import webui

_studios: list = []
_orig_init = webui.Studio.__init__


def _tracking_init(self, *a, **k):
    _orig_init(self, *a, **k)
    _studios.append(self)


webui.Studio.__init__ = _tracking_init


@pytest.fixture(autouse=True)
def _stop_pumps():
    yield
    while _studios:
        st = _studios.pop()
        st.released = True
        st.last_view_req = 0.0


@pytest.fixture(autouse=True)
def _never_his_real_key(monkeypatch, tmp_path_factory):
    """No test ever reads his real Claude key: the saved one points at an empty temp file, the
    environment fallback at nothing (tests that need a key set a fake one themselves)."""
    from camctl import assistant, keystore
    d = tmp_path_factory.mktemp("key")
    monkeypatch.setattr(keystore, "BASE", d)
    monkeypatch.setattr(keystore, "PATH", d / "claude_key.bin")
    import os
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)              # his real one, if set, is never seen
    monkeypatch.setattr(assistant, "_env_key", lambda: os.environ.get("ANTHROPIC_API_KEY", "").strip())   # no registry


@pytest.fixture(autouse=True)
def _operator_files_in_temp(monkeypatch, tmp_path_factory):
    """The Operator's setup stamp, last-setup values and event log never touch his real %APPDATA%."""
    from camctl import autofix
    d = tmp_path_factory.mktemp("op")
    monkeypatch.setattr(autofix, "_stamp_file", lambda: d / "last_auto_setup")


@pytest.fixture(autouse=True)
def _never_his_real_connection(monkeypatch, tmp_path_factory):
    """Tests never read or write his real connection settings, and never scan the real USB bus."""
    from camctl import wifi
    import camctl.ptpusb as ptpusb
    d = tmp_path_factory.mktemp("wifi")
    monkeypatch.setattr(wifi, "BASE", d)
    monkeypatch.setattr(wifi, "CONFIG", d / "wifi.json")
    monkeypatch.setattr(ptpusb, "find_ptp_cameras", lambda *a, **k: [])
    monkeypatch.setattr(ptpusb, "cameras_on_usb", lambda *a, **k: [])          # never scan the real USB bus


@pytest.fixture(autouse=True)
def _camera_profiles_in_temp(monkeypatch, tmp_path_factory):
    """Saved camera profiles (check results, confirmed log profiles) never touch his real %APPDATA%."""
    from camctl import camprofile
    d = tmp_path_factory.mktemp("prof")
    monkeypatch.setattr(camprofile, "BASE", d)
    monkeypatch.setattr(camprofile, "SAVED", d / "camera_profiles.json")


@pytest.fixture(autouse=True)
def _claude_model_choice_in_temp(monkeypatch, tmp_path_factory):
    """His Claude model choice (prefs) is never read from or written to his real %APPDATA%."""
    from camctl import assistant
    d = tmp_path_factory.mktemp("prefs")
    monkeypatch.setattr(assistant, "_prefs_file", lambda: d / "prefs.json")
