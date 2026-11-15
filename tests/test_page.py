"""The Studio page must always run: a JavaScript syntax error leaves the owner with a black screen."""
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


def _scripts():
    html = PAGE.read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"<script>(.*?)</script>", html, re.S)]


def _parse_errors(scripts):
    node = shutil.which("node")
    if node:
        out = []
        for s in scripts:
            r = subprocess.run([node, "--check", "-"], input=s.encode(), capture_output=True, timeout=30)
            if r.returncode:
                out.append(r.stderr.decode()[:300])
        return out
    edge = next((e for e in EDGES if Path(e).exists()), None)
    if not edge:
        pytest.skip("no JavaScript engine (node / Chrome / Edge) to check the page")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "check.html"
        f.write_text("<html><body><pre id=r></pre><script>const S=" + json.dumps(scripts) + ";"
                     "document.getElementById('r').textContent=S.map(s=>{try{new Function(s);return 'OK'}"
                     "catch(e){return 'ERR '+e.message}}).join('|');</script></body></html>", encoding="utf-8")
        r = subprocess.run([edge, "--headless=new", *browser.HEADLESS, "--disable-gpu", f"--user-data-dir={d}\\p", "--dump-dom",
                            f.as_uri()], capture_output=True, timeout=60)
        dom = r.stdout.decode("utf-8", "replace")
        m = re.search(r'<pre id="r">(.*?)</pre>', dom, re.S)
        assert m, "could not run the check page"
        return [x for x in m.group(1).split("|") if x != "OK"]


def test_page_scripts_parse():
    scripts = _scripts()
    assert scripts
    assert _parse_errors(scripts) == []


def test_a_broken_script_is_caught():
    assert _parse_errors(["(async()=>{ try{ x=1;   // comment eats the rest }catch(e){} })();"])


def test_offline_guide_does_not_depend_on_the_camera():
    s = "".join(_scripts())
    boot = s[s.index("/* ------------------------------------------------------------ boot */"):]
    assert boot.index("setSimple(") < boot.index('api("/api/catalog")')   # layout first, then any camera call


def _run_js(src: str) -> str:
    edge = next((e for e in EDGES if Path(e).exists()), None)
    if not edge:
        pytest.skip("no Chrome or Edge to run the page logic")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "run.html"
        f.write_text("<html><body><pre id=r></pre><script>try{document.getElementById('r').textContent=(function(){" + src +
                     "})();}catch(e){document.getElementById('r').textContent='ERR '+e.message}</script></body></html>", encoding="utf-8")
        r = subprocess.run([edge, "--headless=new", *browser.HEADLESS, "--disable-gpu", f"--user-data-dir={d}\\p", "--dump-dom", f.as_uri()],
                           capture_output=True, timeout=60)
        m = re.search(r'<pre id="r">(.*?)</pre>', r.stdout.decode("utf-8", "replace"), re.S)
        return m.group(1) if m else ""


def test_before_after_pairs_are_fair():
    """Before/after: same mode, same monitor view, same aspect; otherwise no picture comparison."""
    src = "".join(_scripts())
    fn = re.search(r"function baFair\(b,a\)\{.*?\n  return \"\"; \}", src, re.S).group(0)
    js = fn + """
      const p=(o)=>Object.assign({src:'x',t:0,mode:'photo',view:'log',w:1024,h:576},o);
      return JSON.stringify([baFair(p({}),p({t:3000})), baFair(p({}),p({mode:'video'})), baFair(p({}),p({view:'709'})),
                             baFair(p({}),p({w:1024,h:683})), baFair(p({}),null)]);"""
    assert json.loads(_run_js(js)) == ["", "mode", "view", "aspect", "no picture"]


def test_before_after_uses_camera_frames_and_a_new_pair_per_setup():
    src = "".join(_scripts())
    snap = src[src.index("function snap(){"):src.index("async function freshFrames")]
    assert "S.lastFrame" in snap and "toDataURL" in snap            # the camera's frame, not the drawn monitor
    run = src[src.index("async function runAuto(o){"):src.index("function verdictHtml")]
    assert run.index("S.baPair=null") < run.index("shotMeta()")          # an old pair is discarded first
    assert "freshFrames()" in run                                         # after = settled picture
    html = PAGE.read_text(encoding="utf-8")
    assert html.index('>Before</span>') < html.index('>After</span>') and 'id="baSetB"' in html and 'id="baSetA"' in html


def test_tap_maps_to_the_camera_frame_in_every_layout():
    """Tap position -> frame position, for contain + centred (laptop/iPad landscape) and contain + top (upright)."""
    src = "".join(_scripts())
    fn = src[src.index("function picRect(){"):src.index("async function tapFocus")]
    js = """
      const cv=document.createElement('canvas'); cv.width=1024; cv.height=576; document.body.style.margin='0';
      cv.style.cssText='position:absolute;left:0;top:0;width:820px;height:1180px;object-fit:contain'; document.body.append(cv);
      """ + fn + """
      const at=(x,y)=>{ const p=frameCoords({clientX:x,clientY:y}); return p?[+p.x.toFixed(3),+p.y.toFixed(3)]:null; };
      const out={};
      out.portraitCentred=at(410,590);                   // centre of the box = centre of the frame
      cv.style.objectPosition='50% 70px';                 // upright layout: picture at the top
      out.portraitTopLeft=at(0,70); out.portraitBottomRight=at(819.9,70+819.9*576/1024-0.1); out.portraitOutside=at(410,1000);
      cv.style.cssText='position:absolute;left:0;top:0;width:1366px;height:768px;object-fit:contain';
      out.laptopCentre=at(683,384); out.laptopEdge=at(1,384);
      return JSON.stringify(out);"""
    out = json.loads(_run_js(js))
    assert out["portraitCentred"] == [0.5, 0.5]
    assert out["portraitTopLeft"] == [0.0, 0.0] and out["portraitBottomRight"][0] > 0.99 and out["portraitBottomRight"][1] > 0.99
    assert out["portraitOutside"] is None
    assert out["laptopCentre"] == [0.5, 0.5] and out["laptopEdge"][0] < 0.01


def test_every_tap_focuses_and_reports_not_found():
    src = "".join(_scripts())
    tap = src[src.index("async function tapFocus(ev){"):src.index("function drawHist(H)")]
    assert "idle" not in tap and "justWoke" not in tap                 # no ignored "wake" taps any more
    assert "r.found" in tap and "Couldn't find focus there" in tap      # not found is said in words
    assert tap.index("S.afPoint=p") < tap.index('post("/api/focus/point"')   # the box moves before the camera answers
