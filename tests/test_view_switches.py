"""Every switch in More > See the exposure / Check the focus, and Log | 709, in a real browser engine on the
simulated camera: it must visibly change the picture (pixels, size, on top) and turn off cleanly."""
import asyncio
import json
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from camctl import browser

EDGES = [p for p in [browser.find()] if p]          # Chrome, else Edge, else Chromium (camctl/browser.py)
websockets = pytest.importorskip("websockets")


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    edge = next((e for e in EDGES if Path(e).exists()), None)
    if not edge:
        pytest.skip("no Chrome or Edge to drive the page")
    import uvicorn
    from camctl import handoff, webui
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    tmp = tmp_path_factory.mktemp("sw")
    webui.setups.BASE, webui.CAPTURE_DIR = tmp, tmp / "captures"
    handoff.SHOOTS, handoff.STATE = tmp / "shoots", tmp / "shoot.json"
    cam = ZVE10(ev=13, mode="photo", highlight=3.0)       # a bright window so zebras have something to mark
    st = _studio_with(cam)
    app = webui.build_app(st, start_auto=False) if "start_auto" in webui.build_app.__code__.co_varnames else webui.build_app(st)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    dport = _port()
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen([edge, "--headless=new", *browser.HEADLESS, "--disable-gpu", f"--remote-debugging-port={dport}", f"--user-data-dir={prof}", "about:blank"])
    for _ in range(80):
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json", timeout=1).read())
            ws_url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    loop = asyncio.new_event_loop()
    ws = loop.run_until_complete(websockets.connect(ws_url, max_size=50_000_000))
    n = [0]
    errors = []

    async def _cmd(method, **params):
        n[0] += 1
        await ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("method") == "Runtime.exceptionThrown":
                errors.append(m["params"]["exceptionDetails"].get("exception", {}).get("description", "")[:200])
            if m.get("id") == n[0]:
                return m.get("result", {})

    def js(expr):
        r = loop.run_until_complete(_cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True))
        return r.get("result", {}).get("value")

    loop.run_until_complete(_cmd("Runtime.enable"))
    loop.run_until_complete(_cmd("Emulation.setDeviceMetricsOverride", width=1366, height=768, deviceScaleFactor=1, mobile=False))
    loop.run_until_complete(_cmd("Page.navigate", url=f"http://127.0.0.1:{port}/"))
    time.sleep(6)
    js("typeof rdClose==='function' && rdClose(); true")      # the Getting ready card (no key in tests) is not under test here
    js("if(typeof cpClose==='function'){ CP.start=false; cpClose(); } true")   # nor the "Your camera" start screen
    js(HELPERS)
    yield js, errors, cam
    loop.run_until_complete(ws.close())
    proc.terminate()
    server.should_exit = True


HELPERS = r'''
window.T = {
  px(){ const c=document.querySelector("#cv"); const d=c.getContext("2d").getImageData(0,0,c.width,c.height).data;
    let zeb=0, fc=0, sum=0, sq=0, n=0; const FCS=["110,0,160","20,60,220","40,200,80","255,60,60"];
    for(let i=0;i<d.length;i+=4){ const k=d[i]+","+d[i+1]+","+d[i+2]; if(k==="230,40,200") zeb++; if(FCS.includes(k)) fc++;
      const l=(d[i]*54+d[i+1]*183+d[i+2]*19)>>8; sum+=l; sq+=l*l; n++; }
    const m=sum/n; return {zeb, fc, mean:m, sd:Math.sqrt(sq/n-m*m), w:c.width, h:c.height, sig:sum}; },
  async wait(ms){ await new Promise(r=>setTimeout(r,ms)); return true; },
  async more(label){ document.querySelector("#moreSheet").classList.add("on"); renderMore(); await T.wait(300);
    const b=[...document.querySelectorAll("#mGroups .mrow")].find(x=>x.textContent.trim().startsWith(label));
    if(!b) return "no row "+label; b.click(); await T.wait(200); document.querySelector("#moreSheet").classList.remove("on"); await T.wait(1200); return "ok"; },
  visible(sel){ const e=document.querySelector(sel); if(!e) return {ok:false}; const r=e.getBoundingClientRect();
    const top=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
    return {ok:r.width>0&&r.height>0&&getComputedStyle(e).display!=="none"&&getComputedStyle(e).visibility!=="hidden", w:r.width, h:r.height,
            onTop:!!top&&(e===top||e.contains(top)||top.contains(e)), bottom:r.bottom, vh:innerHeight, right:r.right, vw:innerWidth}; },
  inkPixels(sel){ const c=document.querySelector(sel); const d=c.getContext("2d").getImageData(0,0,c.width,c.height).data; let k=0;
    for(let i=0;i<d.length;i+=4) if(d[i+3]>0 && (d[i]+d[i+1]+d[i+2])>60) k++; return k; }
};
true
'''


def test_false_colour_on_and_off(page):
    js, errors, _ = page
    before = js("T.px()")
    assert js("T.more('False colour')") == "ok"
    on = js("T.px()")
    assert on["fc"] > before["fc"] + 500, (before, on)          # the picture is painted in false colour
    assert js("T.more('False colour')") == "ok"
    off = js("T.px()")
    assert off["fc"] < on["fc"] / 4
    assert not errors


def test_zebras_on_and_off(page):
    js, errors, _ = page
    assert js("T.more('Zebras')") == "ok"
    on = js("T.px()")
    assert on["zeb"] > 200, on                                  # stripes over the bright window
    assert js("T.more('Zebras')") == "ok"
    assert js("T.px()")["zeb"] == 0
    assert not errors


def test_waveform_is_visible_on_top_and_turns_off(page):
    js, errors, _ = page
    assert js("T.more('Waveform')") == "ok"
    v = js("T.visible('#dzScope')")
    assert v["ok"] and v["w"] >= 200 and v["h"] >= 80 and v["onTop"], v
    assert v["bottom"] <= v["vh"] and v["right"] <= v["vw"]
    assert js("T.inkPixels('#wave')") > 500                      # the trace and the scale are drawn
    assert js("T.more('Waveform')") == "ok"
    assert not js("T.visible('#dzScope')")["ok"]
    assert not errors


def test_magnify_for_six_seconds(page):
    js, errors, _ = page
    before = js("T.px()")
    assert js("T.more('Magnify for 6 seconds')") == "ok"
    on = js("T.px()")
    assert js("S.mag") > 1 and abs(on["sig"] - before["sig"]) > before["sig"] * 0.02, (before, on)
    js("T.wait(6500)")
    assert js("S.mag") == 1
    assert not errors


def test_one_to_one_view_and_back(page):
    js, errors, _ = page
    fit = js("(()=>{const r=document.querySelector('#cv').getBoundingClientRect(); return [r.width,r.height]})()")
    assert js("T.more('1:1 view')") == "ok"
    one = js("(()=>{const c=document.querySelector('#cv'), r=c.getBoundingClientRect(); return [r.width, c.width/(devicePixelRatio||1)]})()")
    assert abs(one[0] - one[1]) < 2 and abs(one[0] - fit[0]) > 5, (fit, one)
    assert js("T.more('1:1 view')") == "ok"
    back = js("(()=>{const r=document.querySelector('#cv').getBoundingClientRect(); return [r.width,r.height]})()")
    assert abs(back[0] - fit[0]) < 2
    assert not errors


def test_log_709_switch_in_video(page):
    js, errors, cam = page
    js("post('/api/set',{name:'exposure_mode',value:491603}).then(()=>post('/api/set',{name:'picture_profile',value:8}))"
       ".then(()=>loadSettings()).then(()=>{ S.mode='video'; renderFormat(); return true; })")   # S-Log3 as recorded
    js("T.wait(2500)")
    assert js("T.visible('#sx709')")["ok"]
    log = js("T.px()")
    js("document.querySelector('#sx709 [data-v=\"709\"]').click()")
    js("T.wait(1500)")
    v709 = js("T.px()")
    assert abs(v709["mean"] - log["mean"]) > 5 or abs(v709["sd"] - log["sd"]) > log["sd"] * 0.2, (log, v709)   # the picture visibly changes
    js("document.querySelector('#sx709 [data-v=\"log\"]').click()")
    js("T.wait(1500)")
    back = js("T.px()")
    assert abs(back["mean"] - log["mean"]) < 3 and abs(back["sd"] - log["sd"]) < log["sd"] * 0.15 + 0.5, (log, back)
    assert not errors


def test_battery_time_left_amber_and_series_guard(page):
    js, errors, cam = page
    js("BAT.samples=[]; S.link='WI-FI'; S.batt=46; renderDz(); true")
    assert js("document.querySelector('#dzBattT').textContent") == "46% · 55 min"           # first estimate: 50 %/h (Wi-Fi + live view)
    js("BAT.samples=[]; S.batt=11; renderDz(); true")                                      # about 19 minutes left
    assert "low" in js("document.querySelector('#dzBatt').className")                      # amber at about 20 minutes
    cam.common[0xD218].value = 1                                                         # the camera reports 1 %
    js("BAT.samples=[]; loadSettings().then(()=>{ spSheet('burst'); return true; })")                                   # about 2 minutes left, the series takes about 1
    js("T.wait(600)")
    js("spStart()")
    js("T.wait(400)")
    assert "may not last" in js("document.querySelector('#trCap').textContent")
    assert js("document.querySelector('#spStart').textContent") == "Start anyway"
    assert not js("!!(S.series&&S.series.running)")                                      # nothing started without saying so
    cam.common[0xD218].value = 80
    js("closeTray(); true")
    assert not errors
