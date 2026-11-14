"""UI pass round 2 (13 Nov 2026): the fixes that are not only layout.

- A Claude request is never sent without a key (it could only fail as "wrong key" and leave that sentence behind).
- The simulator with --fake-claude shows Claude "with a key" (pretend, in memory) but never makes a request, never
  reads the Windows key setting, and never ends up saying "Claude didn't accept the key" (R16).
- The page: one picture in the photo review, a plain photo viewer, toasts on a dialog's own line, the take card
  giving way to every other surface, nothing to analyse or apply while recording.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camctl import assistant  # noqa: E402

PAGE = (ROOT / "camctl" / "studio_pro.html").read_text(encoding="utf-8")


def test_no_request_without_a_key(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a request was sent")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(assistant.AskError) as e:
        assistant._send({"model": "x", "messages": []}, "")
    assert str(e.value) == "NO_KEY"


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_fake_claude_simulator_never_asks_claude(tmp_path):
    port = _port()
    env = {**os.environ, "APPDATA": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "sim_studio.py"), "--port", str(port), "--fake-claude"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    get = lambda p: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=10).read())  # noqa: E731
    try:
        for _ in range(100):
            try:
                get("/api/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        time.sleep(9)                                   # the Operator's first-connect setup (it may try Claude) runs
        st = get("/api/claude/state")
        assert st["has_key"] is True and st["env_too"] is False     # pretend key; the Windows setting is not even looked at
        assert st["problem"] is None
        rd = get("/api/ready")
        assert rd["claude"]["problem"] is None                       # never "Claude didn't accept the key"
    finally:
        proc.terminate()
        proc.wait(10)


def test_sim_blocks_every_request():
    src = (ROOT / "tools" / "sim_studio.py").read_text(encoding="utf-8")
    assert "webui.assistant._send = _no_network" in src
    assert "webui.assistant._env_key = lambda: \"\"" in src
    assert "webui.CAMCHECK_AUDIT = tmp / \"AUDIT.md\"" in src          # a simulated camera check stays out of AUDIT.md


def test_photo_review_is_one_picture_and_the_viewer_is_plain():
    assert 'id="rvCard" data-n="1"' in PAGE and '$("#rvGP").hidden=true' in PAGE      # single photo: one pane
    assert '$("#rvCard").dataset.n="2"' in PAGE                                        # two panes only for a merged series
    assert '$("#ba").dataset.cmp="0"' in PAGE and '#viewer .ba[data-cmp="0"] .after' in PAGE
    assert "#viewer{background:#000" in PAGE                                          # nothing of the main screen shows through


def test_toast_goes_on_the_dialog_line_and_surfaces_never_stack():
    assert 'querySelectorAll(".result.on,.modal.on")' in PAGE and "[data-toast]" in PAGE
    assert "ONE SURFACE AT A TIME" in PAGE
    assert 'body[data-rec="1"] #dzAgain,body[data-rec="1"] #proposal{display:none!important}' in PAGE
    assert "Claude waits until the setup is finished." in PAGE
