import io

from camctl import grade, presets
from camctl.drivers.sony import PROPS, PropInfo

APERTURES = [350, 400, 450, 500, 560, 630, 710, 800, 900, 1000, 1100]
SHUTTERS = [(1 << 16) | d for d in (4, 8, 15, 30, 50, 60, 100, 125, 250, 500, 1000, 2000)]
ISOS = [100, 200, 400, 800, 1600, 3200, 6400, 12800, 0xFFFFFF]


def props(ap=350, sh=(1 << 16) | 60, iso=800):
    return {PROPS["aperture"]: PropInfo(PROPS["aperture"], 4, True, True, ap, choices=APERTURES),
            PROPS["shutter_speed"]: PropInfo(PROPS["shutter_speed"], 6, True, True, sh, choices=SHUTTERS),
            PROPS["iso"]: PropInfo(PROPS["iso"], 6, True, True, iso, choices=ISOS)}


def test_catalog_complete():
    cat = presets.catalog()
    assert {p["key"] for p in cat["photo"]} >= {"portrait", "landscape", "action", "lowlight", "bw", "night"}
    assert {p["key"] for p in cat["video"]} >= {"vlog", "cinematic", "interview"}
    assert len(cat["looks"]) == 10 and all("lighting" in l and "grade" in l for l in cat["looks"])
    assert all(p.shutter_fixed == 0.02 for p in presets.PRESETS.values() if p.movie and not p.mode)
    assert presets.PRESETS["slowmo"].mode == presets.MODE_SQ_M
    assert all(p.picture_profile == 0 for p in presets.PRESETS.values() if p.section == "photo")


def test_portrait_correctly_exposed_keeps_iso_low():
    # Live view already at target brightness with f/3.5 1/60 ISO 800 -> 1/125 at ISO 1600
    out = presets.solve(presets.PRESETS["portrait"], props(), measured=118)
    assert out["aperture"] == 350
    assert presets.shutter_seconds(out["shutter_speed"]) == 1 / 125
    assert out["iso"] == 1600


def test_dark_scene_slows_shutter_before_extreme_iso():
    out = presets.solve(presets.PRESETS["portrait"], props(), measured=30)   # ~4 stops under
    t = presets.shutter_seconds(out["shutter_speed"])
    assert t <= 1 / 60 + 1e-9          # never slower than the portrait's 1/60 limit
    assert out["iso"] == 12800         # the rest comes from ISO
    out = presets.solve(presets.PRESETS["action"], props(), measured=30)
    assert presets.shutter_seconds(out["shutter_speed"]) <= 1 / 500 + 1e-9


def test_video_keeps_180_degree_shutter_and_landscape_f8():
    out = presets.solve(presets.PRESETS["cinematic"], props(), measured=200)
    assert presets.shutter_seconds(out["shutter_speed"]) == 1 / 50
    out = presets.solve(presets.PRESETS["landscape"], props(), measured=118)
    assert out["aperture"] == 800


def test_grade_identity_mono_and_jpeg():
    assert grade.grade_pixel(0.2, 0.5, 0.8, {}) == (0.2, 0.5, 0.8)
    r, g, b = grade.grade_pixel(0.9, 0.2, 0.1, {"mono": True})
    assert abs(r - g) < 1e-9 and abs(g - b) < 1e-9
    half = grade.grade_pixel(0.9, 0.2, 0.1, {"mono": True}, strength=0.5)
    assert 0.2 < half[1] < r
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (200, 120, 60)).save(buf, "JPEG")
    for look in presets.LOOKS.values():
        out = grade.apply_to_jpeg(buf.getvalue(), look["grade"])
        assert out[:2] == b"\xff\xd8"
