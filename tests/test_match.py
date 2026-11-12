"""Match this shot: the four numbers and the sentence point the right way on synthetic pictures."""
import cv2
import numpy as np

from camctl import match


def scene(w=960, h=540, dx=0, dy=0, scale=1.0, gain=1.0, warm=0.0, seed=1):
    rng = np.random.default_rng(seed)
    im = np.full((h, w, 3), 110, np.float32)
    for _ in range(40):                                   # blobs and boxes to line up on
        x, y = rng.integers(0, w), rng.integers(0, h)
        c = rng.integers(30, 230, 3).tolist()
        cv2.rectangle(im, (int(x), int(y)), (int(x + rng.integers(20, 120)), int(y + rng.integers(20, 120))), c, -1)
    cv2.circle(im, (w // 2, h // 2), 90, (150, 170, 200), -1)    # "the subject"
    M = cv2.getRotationMatrix2D((w / 2, h / 2), 0, scale)
    M[0, 2] += dx * w
    M[1, 2] += dy * h
    im = cv2.warpAffine(im, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    im = im * gain
    im[..., 2] += warm                                    # BGR: more red
    im[..., 0] -= warm
    return np.clip(im, 0, 255).astype(np.uint8)


def jpg(im):
    return cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


REF = match.prepare(jpg(scene()))


def test_same_picture_is_a_match():
    r = match.compare(REF, jpg(scene()))
    assert min(r["scores"].values()) >= 90, r
    assert r["say"].startswith("That's a match")


def test_shift_says_which_way_to_turn():
    r = match.compare(REF, jpg(scene(dx=0.12)))         # content moved right in the live picture
    assert r["scores"]["framing"] < 85, r
    assert r["raw"]["dx"] > 0.08, r
    assert "right" in r["say"], r["say"]
    r = match.compare(REF, jpg(scene(dy=-0.12)))
    assert "up" in r["say"], r["say"]


def test_size_says_closer_or_back():
    r = match.compare(REF, jpg(scene(scale=1.3)))
    assert r["raw"]["scale"] > 1.15 and "Step back" in r["say"], r
    r = match.compare(REF, jpg(scene(scale=0.75)))
    assert r["raw"]["scale"] < 0.85 and "closer" in r["say"], r


def test_brightness_and_colour():
    r = match.compare(REF, jpg(scene(gain=0.5)))
    assert r["raw"]["stops"] < -0.6 and r["scores"]["framing"] >= 80, r
    assert "darker" in r["say"] and "add light" in r["say"], r["say"]
    r = match.compare(REF, jpg(scene(warm=25)))
    assert r["raw"]["warm"] > 5 and "Warmer" in r["say"] and "lower" in r["say"], r


def test_log_monitor_is_compared_as_709():
    """A flat S-Log3 live picture of the same scene must not read as 'flatter' once converted."""
    from camctl.grade import slog3_to_linear  # noqa: F401  (the conversion the LUT uses)
    ref709 = scene()
    # encode the reference's linear light into S-Log3 code values (inverse of the view LUT, per channel)
    lut = match._log_lut().astype(np.int32)
    inv = np.zeros(256, np.uint8)
    for v in range(256):
        inv[v] = int(np.argmin(np.abs(lut - v)))
    log_live = cv2.LUT(ref709, inv)
    flat = match.compare(REF, jpg(log_live), log=False)
    fair = match.compare(REF, jpg(log_live), log=True)
    assert fair["scores"]["contrast"] > flat["scores"]["contrast"], (flat, fair)
    assert fair["scores"]["contrast"] >= 85, fair


def test_simulator_scene_zoom_shift_and_warmth():
    """The simulator's plain scene (one face, flat wall): zoom, shift and warmth are found, and it is fast."""
    import time
    from zve10_sim import ZVE10
    cam = ZVE10(ev=8)
    im = cv2.resize(cv2.imdecode(np.frombuffer(cam.live_view(), np.uint8), cv2.IMREAD_COLOR), (1280, 720)).astype(np.float32)
    M = cv2.getRotationMatrix2D((640, 360), 0, 1.18)
    M[0, 2] -= 128
    im = cv2.warpAffine(im, M, (1280, 720), borderMode=cv2.BORDER_REFLECT)
    im[..., 2] += 14
    im[..., 0] -= 14
    ref = match.prepare(jpg(np.clip(im, 0, 255).astype(np.uint8)))
    match.compare(ref, cam.live_view())
    t = time.time()
    r = match.compare(ref, cam.live_view())
    assert time.time() - t < 0.4
    assert abs(r["raw"]["scale"] - 1 / 1.18) < 0.04 and r["raw"]["dx"] > 0.05 and r["raw"]["warm"] < -2, r
    assert "closer" in r["say"], r["say"]
