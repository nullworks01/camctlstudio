"""The release gate on the simulator: the page loads with no errors and every main control opens; Analyse again runs
one setup and ends Locked; a second press while it runs does not queue."""
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
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def sim(tmp_path):
    port = _port()
    env = {**os.environ, "APPDATA": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "sim_studio.py"), "--port", str(port), "--ev", "10"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1); break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    time.sleep(6)                                         # the first-connect setup finishes
    yield port
    proc.terminate(); proc.wait(10)


@pytest.mark.skipif(EDGE is None, reason="Edge not installed")
def test_page_gate_on_the_simulator(sim):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_page.py"), "--port", str(sim), "--press-again"],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Analyse again pressed" in r.stdout


def test_analyse_again_twice_is_one_setup(sim):
    def post(b):
        req = urllib.request.Request(f"http://127.0.0.1:{sim}/api/autofix", json.dumps(b).encode(), {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    def events():
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{sim}/api/autofix", timeout=10).read())["events"]
    n0 = sum(1 for e in events() if "Setup started by your tap" in e["text"])
    post({"analyse": True}); post({"analyse": True})       # a double press
    time.sleep(8)
    n1 = sum(1 for e in events() if "Setup started by your tap" in e["text"])
    assert n1 - n0 == 1
