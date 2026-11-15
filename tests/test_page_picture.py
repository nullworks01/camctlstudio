"""The picture in Studio is the camera's own picture, in every format; the recorded shape is shown, never stretched;
every address the page calls exists; every new control has exactly one home. Real page in headless Chrome (else Edge) on the
simulator (no real camera)."""
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from camctl import browser

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "camctl" / "studio_pro.html"
EDGE = browser.find()          # Chrome, else Edge, else Chromium (camctl/browser.py)


def _port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    port = _port()
    env = {**os.environ, "APPDATA": str(tmp_path_factory.mktemp("appdata"))}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "sim_studio.py"), "--port", str(port), "--ev", "10"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1); break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    time.sleep(6)
    yield port
    proc.terminate(); proc.wait(10)


def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


async def _page(port, steps):
    import websockets
    dport = _port()
    proc = subprocess.Popen([EDGE, "--headless=new", *browser.HEADLESS, "--disable-gpu", f"--remote-debugging-port={dport}",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"])
    out = []
    try:
        for _ in range(80):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json", timeout=1).read())
                url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]; break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.25)
        async with websockets.connect(url, max_size=50_000_000) as ws:
            n = 0

            async def js(e):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                                          "params": {"expression": e, "awaitPromise": True, "returnByValue": True}}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == n:
                        res = r.get("result", {})
                        if res.get("exceptionDetails"):
                            return "ERR " + res["exceptionDetails"].get("exception", {}).get("description", "")[:300]
                        return res.get("result", {}).get("value")
            n += 1
            await ws.send(json.dumps({"id": n, "method": "Page.navigate", "params": {"url": f"http://127.0.0.1:{port}/"}}))
            await asyncio.sleep(6)
            await js("try{ sessionStorage.setItem('camctl.ready','1'); rdClose && rdClose(); }catch(e){} true")
            for s in steps:
                if callable(s):
                    s()
                    await asyncio.sleep(1.5)
                else:
                    out.append(await js(s))
    finally:
        proc.terminate()
    return out


# compares what Studio drew with the frame as received (sampled points, within JPEG/canvas rounding)
SAME = """(async()=>{ await loadSettings(); S.overlays=new Set(VIEW); await sleep(300);
  const r=await fetch('/api/liveview.jpg?after=-1&t='+Date.now()); const bmp=await createImageBitmap(await r.blob());
  const c=document.createElement('canvas'); c.width=bmp.width; c.height=bmp.height; const x=c.getContext('2d'); x.drawImage(bmp,0,0);
  const a=x.getImageData(0,0,c.width,c.height).data;
  drawFrame(bmp); const b=ctx.getImageData(0,0,cv.width,cv.height).data;
  let worst=0; for(let i=0;i<a.length;i+=4*97) for(let k=0;k<3;k++) worst=Math.max(worst,Math.abs(a[i+k]-b[i+k]));
  return {worst, log:!!(S.log&&S.log.active), kind:(S.format||{}).pp_kind, w:cv.width, h:cv.height}; })()"""


@pytest.mark.skipif(EDGE is None, reason="no Chrome or Edge installed")
def test_the_picture_is_the_cameras_own_in_every_profile(sim):
    def pp(v):
        return lambda: _post(sim, "/api/set", {"name": "picture_profile", "value": v})
    plain, v709 = SAME.replace("VIEW", "[]"), SAME.replace("VIEW", '["view709"]')
    r = asyncio.run(_page(sim, [pp(0), plain, v709, pp(10), plain, v709, pp(8), plain, v709]))
    std, std709, hlg, hlg709, log, log709 = r
    assert std["kind"] == "standard" and std["worst"] <= 2 and std709["worst"] <= 2      # no transform at all
    assert hlg["kind"] == "hlg" and hlg["worst"] <= 2 and hlg709["worst"] <= 2           # HLG: shown as it is
    assert log["log"] and log["worst"] <= 2                                               # LOG view: as recorded
    assert log709["worst"] > 20                                                          # 709: only for log


ASPECT = """(async()=>{ await loadSettings(); await sleep(1200); const R=recRect(cv.width,cv.height), p=picRect();
  return {ratio:R.w/R.h, frame:cv.width/cv.height, box:p.w/p.h, masked:R.x>0||R.y>0}; })()"""


@pytest.mark.skipif(EDGE is None, reason="no Chrome or Edge installed")
def test_the_picture_has_the_recorded_shape_and_is_never_stretched(sim):
    def asp(v):
        return lambda: _post(sim, "/api/formats", {"mode": "photo", "key": "aspect", "value": v})
    r = asyncio.run(_page(sim, [asp(1), ASPECT, asp(2), ASPECT, asp(4), ASPECT,
                                "(()=>{ S.guide='9:16'; drawGuides(); const G=guideRect(ov.width,ov.height); return G.w/G.h; })()"]))
    three2, sixteen9, square, guide = r
    assert three2["ratio"] == pytest.approx(1.5, abs=0.02) and not three2["masked"]      # arrives in shape: whole
    assert sixteen9["ratio"] == pytest.approx(16 / 9, abs=0.02) and not sixteen9["masked"]
    assert square["ratio"] == pytest.approx(1.0, abs=0.02) and square["masked"]          # wider than 1:1: masked
    for x in (three2, sixteen9, square):
        assert x["box"] == pytest.approx(x["frame"], abs=0.01)                          # never stretched
    assert guide == pytest.approx(9 / 16, abs=0.02)
    _post(sim, "/api/formats", {"mode": "photo", "key": "aspect", "value": None})


def test_every_address_the_page_calls_exists():
    from camctl import webui
    from test_autopilot import StubCam, _studio_with
    app = webui.build_app(_studio_with(StubCam()))
    paths = [getattr(r, "path", "") for r in app.router.routes]
    pats = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "$") for p in paths if p]
    page = PAGE.read_text(encoding="utf-8")
    called = set(re.findall(r'["`](/api/[A-Za-z0-9_/.\-]+)', page))
    missing = [a for a in sorted(called) if not any(p.match(a.rstrip("/")) or p.match(a) for p in pats)
               and not any(p.pattern.startswith("^" + a) for p in pats)]
    assert not missing, missing
    for a in ("/api/formats", "/api/sound/ask", "/api/sound/cancel"):
        assert a in called


def test_new_controls_have_one_home():
    page = PAGE.read_text(encoding="utf-8")
    assert page.count('mrow("Frame guide"') == 1 and page.count('mrow("Sound check"') == 1
    assert page.count('id="asFormat"') == 1 and page.count('id="asClaudeModelT"') == 1
    assert page.count("Sound check works on the laptop.") == 1
    assert 'data-aid="frame"' not in page                                               # no second frame switch
