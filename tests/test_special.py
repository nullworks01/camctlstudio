"""Special shots: merge quality on synthetic series (no camera), and the series flow on the simulated camera."""
import time

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from camctl import compute  # noqa: E402


def _scene(h=360, w=540, seed=1):
    rng = np.random.default_rng(seed)
    base = (rng.random((h // 6, w // 6, 3)) * 255).astype(np.uint8)
    im = cv2.resize(base, (w, h), interpolation=cv2.INTER_NEAREST)       # crisp detail everywhere
    return cv2.GaussianBlur(im, (3, 3), 0)


def _sharp(im, box):
    x, y, w, h = box
    g = cv2.cvtColor(np.clip(im, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)[y:y + h, x:x + w]
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def test_focus_stack_is_sharp_everywhere(tmp_path):
    sharp = _scene()
    h, w = sharp.shape[:2]
    thirds = [(0, 0, w // 3, h), (w // 3, 0, w // 3, h), (2 * w // 3, 0, w // 3, h)]
    frames = []
    for k in range(3):                                   # each frame sharp in one third only (near / middle / far)
        f = cv2.GaussianBlur(sharp, (0, 0), 4)
        x, y, bw, bh = thirds[k]
        f[y:y + bh, x:x + bw] = sharp[y:y + bh, x:x + bw]
        frames.append(f)
    paths = []
    for i, f in enumerate(frames):
        p = tmp_path / f"s{i}.jpg"
        cv2.imwrite(str(p), f, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(p)
    res = compute.run("focusstack", paths, tmp_path / "out" / "stack", tripod=True)
    out = cv2.imread(res["jpg"]).astype(np.float32)
    ref = [_sharp(sharp.astype(np.float32), b) for b in thirds]
    got = [_sharp(out, b) for b in thirds]
    single = [_sharp(frames[0].astype(np.float32), b) for b in thirds]
    assert all(g > 0.6 * r for g, r in zip(got, ref)), (got, ref)       # sharp in every third
    assert min(single) < 0.2 * min(ref)                                  # one frame alone is not
    assert cv2.imread(res["tif"], cv2.IMREAD_UNCHANGED).dtype == np.uint16


def test_alignment_recovers_handheld_shifts(tmp_path):
    base = _scene(seed=3)
    frames = [base]
    for dx, dy in ((3, -2), (-4, 1), (2, 3)):
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        frames.append(cv2.warpAffine(base, M, (base.shape[1], base.shape[0]), borderMode=cv2.BORDER_REFLECT))
    aligned, q = compute.align(frames)
    assert min(q[1:]) > 0.9
    err = [float(np.mean(np.abs(a[20:-20, 20:-20].astype(np.float32) - base[20:-20, 20:-20]))) for a in aligned[1:]]
    assert max(err) < 6, err


def test_focus_stack_series_on_the_simulated_camera(monkeypatch, tmp_path):
    """Start → N photos near→far → merged result beside the originals; settings and focus position restored."""
    from starlette.testclient import TestClient
    from camctl import handoff, presets, webui
    from camctl.drivers import sony
    from camctl.drivers.sony import PROPS
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    cam = ZVE10(ev=9, mode="photo")
    cam.p[PROPS["focus_mode"]].value = 2
    before = {k: cam.p[PROPS[k]].value for k in ("aperture", "shutter_speed", "iso", "focus_mode")}
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/series/start", json={"kind": "focusstack", "shots": 4, "step": 3, "near": 1}).json()
        assert r["ok"] and r["shots"] == 4
        for _ in range(200):
            s = c.get("/api/series/status").json()
            if not s["running"]:
                break
            time.sleep(0.1)
    assert s["step"] == 4 and s["result"] and s["result"]["name"] == "Focus stack"
    from pathlib import Path
    folder = Path(s["folder"])
    assert len(list(folder.glob("*.jpg"))) >= 4 + 1                       # 4 originals + the merged preview
    assert (folder / "merged focus stack.tif").exists()
    assert getattr(cam, "focus_pos", 0) == 0                              # focus stepped back to where it was
    after = {k: cam.p[PROPS[k]].value for k in ("aperture", "shutter_speed", "iso", "focus_mode")}
    assert after == before                                                # nothing survives the series


def test_clean_night_shot_reduces_noise(tmp_path):
    """A handheld noisy burst (small shifts) → aligned + averaged: noise drops by about sqrt(n)."""
    rng = np.random.default_rng(7)
    clean = cv2.GaussianBlur(_scene(seed=5), (0, 0), 1.2)
    paths = []
    for i in range(8):
        dx, dy = (0, 0) if i == 0 else (int(rng.integers(-3, 4)), int(rng.integers(-3, 4)))
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        f = cv2.warpAffine(clean, M, (clean.shape[1], clean.shape[0]), borderMode=cv2.BORDER_REFLECT).astype(np.float32)
        f = np.clip(f + rng.normal(0, 18, f.shape), 0, 255).astype(np.uint8)              # ISO-like noise
        p = tmp_path / f"b{i}.jpg"
        cv2.imwrite(str(p), f, [cv2.IMWRITE_JPEG_QUALITY, 97])
        paths.append(p)
    res = compute.run("burst", paths, tmp_path / "out" / "clean")
    one = cv2.imread(str(paths[0])).astype(np.float32)
    out = cv2.imread(res["tif"], cv2.IMREAD_UNCHANGED).astype(np.float32) / 257       # the 16-bit file he grades
    c = (slice(30, -30), slice(30, -30))
    err_one = float(np.std(one[c] - clean[c]))
    err_out = float(np.std(out[c] - clean[c]))
    assert err_out < err_one / 1.7, (err_one, err_out)               # 8 JPEG frames: about /1.9 (ideal /2.8)
    assert min(res["aligned_quality"][1:]) > 0.8
    assert any("lower than one photo" in n for n in res["notes"])
