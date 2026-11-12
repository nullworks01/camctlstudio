"""The simulator never writes to his real state files (operator log, setup marker, saved camera state, ...)."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_simulator_leaves_the_real_appdata_alone(tmp_path):
    real = tmp_path / "real_appdata"                       # stands in for his real %APPDATA%
    (real / "camctl" / "logs").mkdir(parents=True)
    log = real / "camctl" / "logs" / "operator.log"
    log.write_text("2026-11-12 03:00:00  setup    an old line that must stay as it is\n", encoding="utf-8")
    before = {p: p.stat().st_mtime for p in real.rglob("*")}
    port = _port()
    env = {**os.environ, "APPDATA": str(real)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "sim_studio.py"), "--port", str(port), "--ev", "10"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        time.sleep(9)                                        # the Operator's first-connect setup runs and logs
        a = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/autofix", timeout=5).read())
        assert any("first connect" in e["text"] for e in a["events"])   # it did log something...
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=10).read()
    finally:
        proc.terminate()
        proc.wait(10)
    after = {p: p.stat().st_mtime for p in real.rglob("*")}
    assert set(after) == set(before), sorted(str(p) for p in set(after) - set(before))   # ...but nowhere real
    assert all(after[p] == before[p] for p in before if p.is_file())
    assert log.read_text(encoding="utf-8").count("\n") == 1
