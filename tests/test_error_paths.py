"""Error paths end in one calm plain sentence (audit 12 Nov 2026): disk full, shoot folder missing, nothing arrived."""
import errno

import pytest


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
    cam = ZVE10(ev=10, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, monkeypatch, webui, handoff


def test_disk_full_is_said_plainly(studio):
    c, cam, mp, webui, handoff = studio
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    mp.setattr(webui.Path, "write_bytes", full)
    r = c.post("/api/shoot")
    assert r.status_code == 507 and r.json()["error"].startswith("The laptop's disk is full")
    assert "Errno" not in r.text and "Traceback" not in r.text


def test_shoot_folder_missing_is_said(studio):
    c, cam, mp, webui, handoff = studio
    def gone(*a, **k):
        raise OSError(errno.ENOENT, "folder missing")
    mp.setattr(handoff, "add_file", gone)
    j = c.post("/api/shoot").json()
    assert j["saved"] and "could not be copied into the shoot folder" in j["note"]
