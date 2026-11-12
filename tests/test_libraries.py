"""One list (requirements.txt) is the single source of truth for what Studio needs. The start check reads it: a missing
required library brings up the Getting ready card; a missing optional one (ffmpeg) never does. Run the suite with the
same python.exe as the real Studio, so a missing library fails loudly here."""
import ast
import importlib
import sys
from pathlib import Path

import pytest

from camctl import needs

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("e", [e for e in needs.entries() if e["required"]], ids=lambda e: e["package"])
def test_required_library_present(e):
    try:
        importlib.import_module(e["module"])
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"A needed library is missing: {e['package']} ({exc}). Run “Install or repair camctl Studio”.")


def test_ffmpeg_found():
    """Video tools need a working ffmpeg (installed, or the one imageio-ffmpeg brings)."""
    from camctl import video
    video._FF.clear()
    if not video.find_ffmpeg():
        pytest.fail(video.FFMPEG_MISSING + " Run “Install or repair camctl Studio”.")


def test_every_library_studio_imports_is_on_the_list():
    listed = {e["module"] for e in needs.entries()}
    used = set()
    for f in list((ROOT / "camctl").rglob("*.py")) + list((ROOT / "tools").glob("*.py*")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8-sig"))):
            if isinstance(n, ast.Import):
                used |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                used.add(n.module.split(".")[0])
    third = {m for m in used if m not in sys.stdlib_module_names and m not in ("camctl", "zve10_sim", "battery_guard")}
    assert third <= listed, f"not in requirements.txt: {sorted(third - listed)}"


def _client(monkeypatch, missing_required=(), missing_optional=(), camera=True):
    from starlette.testclient import TestClient
    from camctl import webui
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    by = {e["module"]: e for e in needs.entries()}
    monkeypatch.setattr(needs, "check", lambda: {"missing_required": [by[m] for m in missing_required],
                                                 "missing_optional": [by[m] for m in missing_optional]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", ("sk-" + "ant-test-") + "Q7x" * 12)          # Claude ready (fake key, never used)
    from camctl import assistant
    monkeypatch.setattr(assistant, "_env_key", lambda: ("sk-" + "ant-test-") + "Q7x" * 12)
    st = _studio_with(ZVE10(ev=10))
    if not camera:
        monkeypatch.setattr(webui.Studio, "health", lambda self: {"state": "absent", "message": "The camera is off."})
    return TestClient(webui.build_app(st), client=("127.0.0.1", 50001))


def test_both_ready_and_ffmpeg_missing_no_card(monkeypatch):
    with _client(monkeypatch, missing_optional=["imageio_ffmpeg"]) as c:
        r = c.get("/api/ready").json()
    assert r["show"] is False and r["libs"]["missing"] == [] and "ffmpeg" in r["libs"]["optional"][0]


def test_camera_missing_and_ffmpeg_missing_card_with_camera_line(monkeypatch):
    with _client(monkeypatch, missing_optional=["imageio_ffmpeg"], camera=False) as c:
        r = c.get("/api/ready").json()
    assert r["show"] and r["camera"]["ok"] is False and r["camera"]["text"] == "The camera is off." and r["libs"]["missing"] == []


def test_required_library_missing_card_shown(monkeypatch):
    with _client(monkeypatch, missing_required=["cv2"]) as c:
        r = c.get("/api/ready").json()
    assert r["show"] and r["libs"]["missing"] == ["opencv-python-headless"] and "face detection" in r["libs"]["text"]


def test_ffmpeg_missing_is_said(monkeypatch):
    from camctl import video
    monkeypatch.setattr(video, "_candidates", lambda: [])
    video._FF.clear()
    try:
        assert video.find_ffmpeg() is None
        with pytest.raises(RuntimeError, match="ffmpeg was not found"):
            video.ffmpeg()
    finally:
        video._FF.clear()
