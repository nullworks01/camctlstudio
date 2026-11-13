"""Playground › Day reel on the simulator: memory only until a photo, one small clip per photo written in the
background, paused while recording, the day's reel with photos fitted (never stretched), delete touches clips only."""
import io
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from camctl import reel, video

FF = video.find_ffmpeg()
needs_ff = pytest.mark.skipif(not FF, reason="ffmpeg not available")


def _jpg(w=320, h=180, v=128):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (v, v, v)).save(b, "JPEG")
    return b.getvalue()


# ---------------------------------------------------------------- the buffer
def test_nothing_is_kept_while_off_and_off_empties_it():
    b = reel.Buffer()
    b.feed(_jpg(), 1.0)
    assert not b.frames
    b.set_on(True)
    b.feed(_jpg(), 1.0)
    assert len(b.frames) == 1
    b.set_on(False)
    assert not b.frames and b.bytes == 0


def test_buffer_holds_three_seconds_and_stays_bounded():
    b = reel.Buffer()
    b.set_on(True)
    for i in range(400):                                   # 10 s at 40 fps
        b.feed(_jpg(), i / 40)
    ts = [t for t, _ in b.frames]
    assert ts[-1] - ts[0] <= reel.SECONDS + 1e-9 and len(b.frames) <= reel.MAX_FRAMES
    big = reel.Buffer()
    big.set_on(True)
    blob = b"\xff\xd8" + b"x" * 3_000_000
    for i in range(30):
        big.feed(blob, i / 30)
    assert big.bytes <= reel.MAX_BYTES                     # memory bound even with huge frames


def test_pause_drops_the_frames():
    b = reel.Buffer()
    b.set_on(True)
    b.feed(_jpg(), 1.0)
    b.pause(True)
    b.feed(_jpg(), 1.1)
    assert not b.frames
    b.pause(False)
    b.feed(_jpg(), 1.2)
    assert len(b.frames) == 1


def test_constant_rate_from_real_times():
    frames = [(i * 0.07, bytes([i])) for i in range(43)]  # about 14 fps, 2.94 s
    seq = reel.constant_rate(frames)
    assert len(seq) == round(42 * 0.07 * 25) + 1
    assert seq[0] == bytes([0]) and seq[-1] == bytes([42])


# ---------------------------------------------------------------- in Studio, on the simulator
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


def _feed(st, seconds=3.2):
    end = time.time() + seconds
    while time.time() < end:
        st.latest_frame(-1, timeout=0.5)                   # keeps the live view pump running
        time.sleep(0.05)


@needs_ff
def test_off_writes_nothing_on_writes_one_clip_per_photo(studio):
    c, cam, st, handoff = studio
    _feed(st, 1.0)
    r = c.post("/api/shoot", json={}).json()
    assert r["saved"]
    shoot = Path(handoff.current()["path"])
    time.sleep(1.0)
    assert not (shoot / "REEL").exists()                   # switch off: nothing written
    assert c.post("/api/reel", json={"on": True}).json()["on"] is True
    _feed(st)
    c.post("/api/shoot", json={})
    for _ in range(100):
        if reel.today(shoot):
            break
        time.sleep(0.1)
    ms = reel.today(shoot)
    assert len(ms) == 1 and ms[0]["photo"] is not None and ms[0]["photo"].suffix.lower() == ".jpg"
    info = video.probe(ms[0]["clip"])
    assert 2.4 <= info["duration"] <= 3.4 and (info["width"], info["height"]) == (320, 180) and not info["audio"]
    s = c.get("/api/reel").json()
    assert s["moments"] == 1 and s["size_mb"] >= 0


def test_the_photo_is_not_delayed_by_the_clip(studio, monkeypatch):
    c, cam, st, handoff = studio
    slow = []
    monkeypatch.setattr(reel, "write_clip", lambda *a, **k: (time.sleep(3), slow.append(1)))
    c.post("/api/reel", json={"on": True})
    _feed(st)
    t0 = time.time()
    c.post("/api/shoot", json={})
    took = time.time() - t0
    assert took < 2.5 and not slow                          # returned while the clip is still being written
    time.sleep(3.5)
    assert slow == [1]


def test_paused_while_recording(studio):
    c, cam, st, handoff = studio
    c.post("/api/reel", json={"on": True})
    st.recording = True
    try:
        _feed(st, 1.0)
        assert not st.reel.frames and c.get("/api/reel").json()["paused"] is True
    finally:
        st.recording = False


def test_refusals_are_calm(studio):
    c, cam, st, handoff = studio
    handoff.ensure_shoot()
    r = c.post("/api/reel/make", json={})
    assert r.status_code == 400 and r.json()["error"].startswith("No moments yet today.")


# ---------------------------------------------------------------- the reel itself
def _clip(path: Path, v: int):
    subprocess.run([FF, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=0x{v:02x}{v:02x}{v:02x}:s=320x180:r=25:d=1", "-pix_fmt", "yuv420p", str(path)],
                   check=True, timeout=60)


def _frame_at(path: Path, t: float):
    import numpy as np
    r = subprocess.run([FF, "-hide_banner", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                        "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=True, timeout=60)
    return np.asarray(Image.open(io.BytesIO(r.stdout)).convert("L")).astype(int)


@needs_ff
def test_reel_order_and_photos_fitted_without_stretch(tmp_path):
    shoot = tmp_path / "shoot"
    clips = shoot / "REEL" / "clips"
    clips.mkdir(parents=True)
    photos = {"3:2": (600, 400), "16:9": (640, 360), "1:1": (400, 400)}
    day = time.strftime("%Y%m%d")
    idx = {}
    for i, (name, (w, h)) in enumerate(photos.items()):
        p = shoot / "PHOTO JPEG" / f"p{i}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (w, h), (255, 255, 255)).save(p, "JPEG", quality=95)
        cn = f"{day}-10000{i}.mp4"
        _clip(clips / cn, 60)
        idx[cn] = {"t": 1000 + i, "photos": [str(p)]}
    import json
    (clips / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    before = {p: p.read_bytes() for p in shoot.rglob("*.jpg")}
    out = reel.make(shoot)
    info = video.probe(out)
    assert (info["width"], info["height"]) == (1280, 720) and not info["audio"]
    assert info["duration"] == pytest.approx(3 * (1.0 + reel.PHOTO_S), abs=0.3)
    # photo frames: 3:2 has side bars, 16:9 fills the frame, 1:1 has wide side bars; the middle is white
    for k, (name, (w, h)) in enumerate(photos.items()):
        f = _frame_at(out, k * (1.0 + reel.PHOTO_S) + 1.0 + 0.7)
        bar = round((1280 - 720 * w / h) / 2) if w / h < 16 / 9 else 0
        assert f[360, 640] > 230                                   # the photo
        if bar > 8:
            assert f[360, bar // 2] < 30 and f[360, 1280 - bar // 2] < 30   # dark bars, not stretched
            assert f[360, bar + 8] > 230
        else:
            assert f[360, 10] > 230
    assert all(p.read_bytes() == b for p, b in before.items())     # photos only read
    n = reel.delete_today(shoot)
    assert n == 3 and not list(clips.glob("*.mp4")) and all(p.exists() for p in before)
    assert out.exists()                                            # the reel itself is kept too


def test_page_has_one_home_for_the_playground():
    page = (Path(__file__).resolve().parents[1] / "camctl" / "studio_pro.html").read_text(encoding="utf-8")
    assert page.count('mrow("Playground"') == 1 and page.count('b.id="pgBox"') == 1
    assert "Experiments. Each one is off until you switch it on, and may change." in page
    assert "Clips are small and silent because they come from the live picture." in page
    assert page.count('d.id="sxReel"') == 1                        # the only thing outside the Playground
