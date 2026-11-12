"""Release gate: load the running Studio page headless in Edge, fail on any page error, open the main controls.

    python tools/check_page.py [--port 8770]

Opens and closes only (More, All settings and each tab, Setups, the shot picker, a number tray, the Claude panel):
nothing is written to the camera. Exit code 0 = the page works, 1 = it does not (the reasons are printed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)

STEPS = [
    ("More", "document.querySelector('#moreSheet').classList.add('on'); renderMore(); true",
     "document.querySelector('#moreSheet').classList.contains('on') && document.querySelectorAll('#mGroups .mrow').length > 3"),
    ("All settings › Camera", "asOpen('camera'); true", "document.body.dataset.as==='1' && !!document.querySelector('#asPanel .as-row')"),
    ("All settings › Light", "asOpen('light'); true", "document.body.dataset.as==='1'"),
    ("All settings › Setups", "asOpen('setups'); true", "!!document.querySelector('#asCards .su-save')"),
    ("All settings › Studio", "asOpen('studio'); true", "!!document.querySelector('#asNeeds')"),
    ("Shot picker", "openTray('shot'); true", "TR.k==='shot' && document.querySelectorAll('#trBody2 .card').length >= 3"),
    ("ISO numbers", "openTray('iso'); true", "TR.k==='iso' && document.querySelectorAll('#trBody2 .stop').length > 3"),
    ("Claude panel", "clOpen(); true", "typeof clIsOpen==='function' && clIsOpen()"),
    ("Analyse again button", "true", "(()=>{ const b=document.querySelector('#dzAgain'); return !!b && b.offsetParent!==null && !b.disabled; })()"),
]
PRESS_AGAIN = ("Analyse again pressed (simulator only)",
               "document.querySelector('#dzAgain').click(); new Promise(r=>setTimeout(r,200)).then(()=>true)",
               "new Promise(r=>{ const t0=Date.now(); (function w(){ if(!S.autoBusy && Date.now()-t0>1500) r(/Locked/.test((S.op||{}).state||'')); "
               "else if(Date.now()-t0>60000) r(false); else setTimeout(w,300); })(); })")
CLOSE = ("(()=>{ try{closeTray()}catch(e){} try{asClose()}catch(e){} try{clClose()}catch(e){} "
         "document.querySelector('#moreSheet').classList.remove('on'); return true; })()")


async def run(port: int, press_again: bool = False) -> list[str]:
    import websockets
    dport = 9361
    proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={dport}",
                             f"--user-data-dir={tempfile.mkdtemp()}", "about:blank"])
    problems: list[str] = []
    try:
        for _ in range(80):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json", timeout=1).read())
                url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.25)
        async with websockets.connect(url, max_size=50_000_000) as ws:
            n = 0

            async def cmd(m, **p):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": m, "params": p}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("method") == "Runtime.exceptionThrown":
                        problems.append("page error: " + r["params"]["exceptionDetails"].get("exception", {}).get("description", "")[:200])
                    if r.get("method") == "Runtime.consoleAPICalled" and r["params"]["type"] == "error":
                        problems.append("console error: " + " ".join(str(a.get("value", a.get("description", "")))[:150] for a in r["params"]["args"]))
                    if r.get("id") == n:
                        return r.get("result", {})

            async def js(e):
                r = await cmd("Runtime.evaluate", expression=e, awaitPromise=True, returnByValue=True)
                if r.get("exceptionDetails"):
                    return "ERR " + r["exceptionDetails"].get("exception", {}).get("description", "")[:200]
                return r.get("result", {}).get("value")

            await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride", width=1366, height=768, deviceScaleFactor=1, mobile=False)
            await cmd("Page.navigate", url=f"http://127.0.0.1:{port}/")
            await asyncio.sleep(5)
            await js("try{ sessionStorage.setItem('camctl.ready','1'); rdClose && rdClose(); }catch(e){} true")
            cam_on = await js("fetch('/api/ready').then(r=>r.json()).then(j=>!!(j.camera&&j.camera.ok))")
            for name, do, ok in STEPS + ([PRESS_AGAIN] if press_again else []):
                if name == "ISO numbers" and cam_on is not True:
                    print(f"--  {name} skipped: no camera connected (needs the camera's values)")
                    continue
                r = await js(do)
                await asyncio.sleep(1.0)
                good = await js(ok)
                if isinstance(r, str) and r.startswith("ERR"):
                    problems.append(f"{name}: {r}")
                elif good is not True:
                    problems.append(f"{name} did not open")
                else:
                    print(f"ok  {name}")
                await js(CLOSE)
                await asyncio.sleep(0.3)
    finally:
        proc.terminate()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--press-again", action="store_true", help="also press Analyse again (simulator only: it runs a setup)")
    a = ap.parse_args()
    if not EDGE:
        print("Edge was not found: the page check cannot run.")
        return 1
    problems = asyncio.run(run(a.port, a.press_again))
    for p in problems:
        print("FAIL " + p)
    print("The page works: every main control opened, no page errors." if not problems else
          f"The page does NOT work ({len(problems)} problem(s)). Fix before calling anything ready.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
