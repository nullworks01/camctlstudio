"""Sound check (More › This shoot): the analysis gives the right plain verdicts on made-up numbers, the sound is never
recorded, saved or sent, and only the numbers reach Claude when he presses the spark."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from camctl import browser

PAGE = Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html"
EDGES = [p for p in [browser.find()] if p]          # Chrome, else Edge, else Chromium (camctl/browser.py)


def _sc_source() -> str:
    t = PAGE.read_text(encoding="utf-8")
    a, b = t.index("const scDb="), t.index("function scDraw(")
    return t[a:b]


def _run_js(src: str, expr: str):
    """Evaluate expr after src in a real JavaScript engine (node, else headless Chrome or Edge); returns the JSON value."""
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "-e", src + f"\nconsole.log(JSON.stringify({expr}));"], capture_output=True, timeout=30)
        return json.loads(r.stdout.decode())
    edge = next((e for e in EDGES if Path(e).exists()), None)
    if not edge:
        pytest.skip("no JavaScript engine")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.html"
        f.write_text("<html><body><pre id=r></pre><script>" + src + "\ndocument.getElementById('r').textContent="
                     f"JSON.stringify({expr});</script></body></html>", encoding="utf-8")
        r = subprocess.run([edge, "--headless=new", *browser.HEADLESS, "--disable-gpu", f"--user-data-dir={d}\\p", "--dump-dom", f.as_uri()],
                           capture_output=True, timeout=60)
        m = re.search(r'<pre id="r">(.*?)</pre>', r.stdout.decode("utf-8", "replace"), re.S)
        import html
        return json.loads(html.unescape(m.group(1)))


CASES = r"""
const fr=(n,v)=>Array(n).fill(v);
const speech=(v,n=120)=>Array.from({length:n},(_,i)=>i%20<14?v:-66);       // talking with short pauses, fast decay
const flat=Array(160).fill(-80);
const humSpec=flat.map((v,i)=>Math.abs(i*2.93-50)<2?-58:v);                // a 50 Hz peak 22 dB above the rest
const echoSpeech=[]; for(let k=0;k<6;k++){ for(let i=0;i<12;i++) echoSpeech.push(-22); for(let i=1;i<=8;i++) echoSpeech.push(-22-i*3.5); }
const R={
 good: scAnalyse({quiet:fr(80,-66),speech:speech(-24),frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:0}),
 noisy: scAnalyse({quiet:fr(80,-40),speech:speech(-20),frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:0}),
 hum: scAnalyse({quiet:fr(80,-62),speech:speech(-24),frameS:0.05,spec:humSpec,binHz:2.93,hz:50,clipped:0}),
 hum60: scAnalyse({quiet:fr(80,-62),speech:speech(-24),frameS:0.05,spec:humSpec,binHz:2.93,hz:60,clipped:0}),
 soft: scAnalyse({quiet:fr(80,-66),speech:speech(-44),frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:0}),
 loud: scAnalyse({quiet:fr(80,-66),speech:speech(-12),frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:40}),
 silent: scAnalyse({quiet:fr(80,-66),speech:fr(120,-65),frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:0}),
 echo: scAnalyse({quiet:fr(80,-66),speech:echoSpeech,frameS:0.05,spec:flat,binHz:2.93,hz:50,clipped:0}),
};
"""


@pytest.fixture(scope="module")
def results():
    return _run_js(_sc_source() + CASES, "R")


def _item(r, key):
    return next((i for i in r["items"] if i["key"] == key), None)


def test_a_good_room_and_voice(results):
    r = results["good"]
    assert _item(r, "noise")["word"] == "quiet" and _item(r, "voice")["word"] == "good"
    assert _item(r, "hum")["word"] == "none" and _item(r, "echo") is None          # echo only when it is clear
    assert r["short"] == "Good"


def test_problems_each_get_one_plain_line_of_advice(results):
    assert _item(results["noisy"], "noise")["word"] == "too noisy"
    assert "fan" in _item(results["noisy"], "noise")["advice"]
    assert _item(results["hum"], "hum")["word"] == "50 Hz hum"
    assert _item(results["hum60"], "hum")["word"] == "none"                         # follows the mains setting
    soft = _item(results["soft"], "voice")
    assert soft["word"] == "too quiet" and "a hand's width from your mouth" in soft["advice"]
    assert _item(results["loud"], "voice")["word"] == "too loud (clipping)"
    assert _item(results["silent"], "voice")["word"] == "no voice heard"
    assert _item(results["echo"], "echo")["word"] == "the room echoes"
    for r in results.values():
        for i in r["items"]:
            assert not re.search(r"error|exception|undefined|NaN", json.dumps(i), re.I)


def test_numbers_only_and_nothing_kept():
    t = PAGE.read_text(encoding="utf-8")
    sc = t[t.index("/* ================= SOUND CHECK"):t.index("(function(){ /* sub-page sources")]
    assert "MediaRecorder" not in sc and "localStorage" not in sc and "indexedDB" not in sc
    assert re.findall(r'post\("([^"]+)"', sc) == ["/api/sound/ask"] and "fetch(" not in sc
    assert 'post("/api/sound/ask",SC.res.numbers)' in sc                            # the numbers, never the sound
    assert "getTracks().forEach(t=>t.stop())" in sc and "ctx.close()" in sc          # microphone released in finally
    assert "Studio cannot hear the camera's own microphone" in sc
    assert "Sound check works on the laptop." in t and "window.isSecureContext" in t


def test_server_sends_only_the_numbers_to_claude(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import assistant, webui
    from test_autopilot import StubCam, _studio_with
    seen = []
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "sk-fake")
    monkeypatch.setattr(assistant, "call", lambda body, key: seen.append(body) or
                        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Move closer."}]})
    st = _studio_with(StubCam())
    with TestClient(webui.build_app(st)) as c:
        r = c.post("/api/sound/ask", json={"noise_dbfs": -52, "voice_dbfs": -30, "hum_db": None, "clipped": 0,
                                           "audio": "UklGRiQAAABXQVZF", "samples": [0.1, 0.2]}).json()
        assert r["say"] == "Move closer." and set(r["sent"]) == {"noise_dbfs", "voice_dbfs", "clipped"}
        text = json.dumps(seen[0])
        assert "UklGR" not in text and "samples" not in text
        big = c.post("/api/sound/ask", content=json.dumps({"audio": "x" * 5000}), headers={"content-type": "application/json"})
        assert big.status_code == 400 and len(seen) == 1
