"""Looks inside Studio: one home, copies of this shoot's files only (originals untouched), the live preview only on his
switch (off at every start), and nothing Studio measures or sends ever sees a look."""
import asyncio
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "camctl" / "studio_pro.html"


@pytest.fixture
def studio(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    cam = ZVE10(ev=9, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        yield c, cam, st, handoff


def test_copy_of_a_photo_leaves_the_original(studio):
    c, cam, st, handoff = studio
    c.post("/api/shoot", json={})
    L = c.get("/api/looks").json()
    assert len(L["looks"]) == 6
    photo = next(f for f in L["files"] if f["kind"] == "photo")
    shoot = Path(handoff.current()["path"])
    src = shoot / photo["file"]
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    for key in ("original", "slide"):
        r = c.get("/api/looks/still", params={"file": photo["file"], "key": key, "strength": 0.6})
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    r = c.post("/api/looks/copy", json={"file": photo["file"], "key": "warmprint", "strength": 0.8}).json()
    assert r["ok"] and "The original is unchanged." in r["say"]
    out = shoot / "LOOKS" / "Warm print" / f"{src.stem} - Warm print.jpg"
    assert out.exists() and hashlib.sha256(src.read_bytes()).hexdigest() == before
    # nothing outside this shoot, never a copy of a copy
    for bad in ("../../shoot.json", "LOOKS/Warm print/" + out.name, "C:/Windows/win.ini"):
        r = c.post("/api/looks/copy", json={"file": bad, "key": "bw"})
        assert r.status_code == 400 and "Traceback" not in r.text


def test_samples_and_tables():
    from starlette.testclient import TestClient
    from camctl import looks, webui
    from test_autopilot import StubCam, _studio_with
    with TestClient(webui.build_app(_studio_with(StubCam()))) as c:
        assert c.get("/api/looks/sample/original.jpg").content[:2] == b"\xff\xd8"
        assert c.get("/api/looks/sample/bw.jpg").content[:2] == b"\xff\xd8"
        assert len(c.get("/api/looks/table/slide").content) == looks.N ** 3 * 3
        assert c.get("/api/looks/table/nothing").status_code == 404


def test_nothing_studio_measures_or_sends_can_see_a_look():
    for mod in ("assistant", "autofix", "capture", "measure", "coach", "reel", "audiocheck", "scene", "director",
                "shotplan", "bestpick", "smartphoto", "wbauto", "followlight"):
        src = (ROOT / "camctl" / f"{mod}.py").read_text(encoding="utf-8")
        assert not re.search(r"from \. import[^\n]*\blooks\b|\blooks\.(transform|table|apply|lut_bytes|copy)", src), mod
    page = PAGE.read_text(encoding="utf-8")
    assert "S.lookPreview=null;" in page and "camctl.look" not in page               # never remembered: off at start
    assert "cv.toDataURL" not in page                                                 # before/after: the camera's frame
    i = page.index("function drawFrame(")
    body = page[i:page.index("\n}\n", i)]
    assert body.index("drawHist(H)") < body.index("applyLook(") and body.index("drawWave(") < body.index("applyLook(")
    assert "const lookOn = !!(S.lookPreview && LK.tab" in body and "if(lookOn) applyLook(" in body   # only by his switch


def test_looks_have_one_home():
    page = PAGE.read_text(encoding="utf-8")
    assert page.count('mrow("Looks"') == 1 and page.count('b.id="lkBox"') == 1 and page.count('id="lkSw"') == 1
    assert page.count('p.id="lookPill"') == 1
    assert "The camera's file is not changed." in page


EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists()), None)

PIX = """(async()=>{ await loadSettings(); const r=await fetch('/api/liveview.jpg?after=-1&t='+Date.now());
  const bmp=await createImageBitmap(await r.blob()); const c=document.createElement('canvas'); c.width=bmp.width; c.height=bmp.height;
  const x=c.getContext('2d'); x.drawImage(bmp,0,0); const a=x.getImageData(0,0,c.width,c.height).data;
  const t=await fetch('/api/looks/table/slide'); LK.tab=new Uint8Array(await t.arrayBuffer());
  S.lookPreview=null; drawFrame(bmp); let b=ctx.getImageData(0,0,cv.width,cv.height).data; const m0=meanLuma;
  let off=0; for(let i=0;i<a.length;i+=4*97) for(let k=0;k<3;k++) off=Math.max(off,Math.abs(a[i+k]-b[i+k]));
  S.lookPreview='slide'; drawFrame(bmp); b=ctx.getImageData(0,0,cv.width,cv.height).data; const m1=meanLuma;
  let on=0; for(let i=0;i<a.length;i+=4*97) for(let k=0;k<3;k++) on=Math.max(on,Math.abs(a[i+k]-b[i+k]));
  S.lookPreview=null; return {off,on,same_hist:m0===m1}; })()"""


from test_page_picture import sim, _page  # noqa: E402,F401  (the simulator fixture and the page driver)


@pytest.mark.skipif(EDGE is None, reason="Edge not installed")
def test_the_preview_only_reaches_the_picture_when_switched_on(sim):
    start, r = asyncio.run(_page(sim, ["S.lookPreview", PIX]))
    assert isinstance(r, dict), r
    assert start is None                                                             # off at every start
    assert r["off"] <= 2 and r["on"] > 10 and r["same_hist"]                          # histogram from the camera frame