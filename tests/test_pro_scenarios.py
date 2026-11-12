"""Professional exposure decisions on the simulated ZV-E10, scenario by scenario (photo RAW and video S-Log3).

Each scenario runs the real app path (POST /api/preset -> run_profile: formats, neutral colour, WB, lens & shutter,
focus, measured exposure, verdict). The expected values are what a photographer / cinematographer would set on
this camera and kit lens (f/3.5 wide): the aperture the shot needs, a safe shutter (photo) or 1/50 at 25p (video),
then the LOWEST ISO that exposes correctly, ND advice when even base ISO is too bright in video.
"""
from __future__ import annotations

import math

import pytest
from starlette.testclient import TestClient

from camctl import capture, measure, presets, webui
from camctl.drivers.sony import PROPS
from zve10_sim import ZVE10
from test_autopilot import _studio_with

# name, profile, mode, EV at the subject, person, extras, expected f-number, expected shutter (s) or None=any,
#   expected ISO range, expected advice keyword (or "")
SCENARIOS = [
    ("bright sun exterior",        "daylight",  "photo", 15, True,  {},                  8.0, (1/1000, 1/250), (100, 100), ""),
    ("open shade portrait",        "portrait",  "photo", 12, True,  {},                  3.5, (1/400, 1/125), (100, 100), ""),
    ("overcast exterior",          "daylight",  "photo", 13, True,  {},                  8.0, (1/250, 1/250), (100, 250), ""),
    ("window-lit interview",       "interview", "photo",  9, True,  {},                  4.0, (1/100, 1/100), (200, 500), ""),
    ("tungsten room (dim)",        "interview", "photo",  6, True,  {},                  4.0, (1/100, 1/100), (1600, 3200), ""),
    ("mixed light room",           "portrait",  "photo",  7, True,  {"highlight": 3},    3.5, (1/125, 1/125), (640, 1600), ""),
    ("product, table + lamp",      "product",   "photo",  8, False, {},                  8.0, (1/60, 1/60),   (800, 2000), ""),
    ("product, tripod, low light", "product",   "photo",  4, False, {},                  5.6, (1/60, 1/60),   (6400, 12800), "tripod"),
    ("backlit subject",            "portrait",  "photo", 10, True,  {"backlight": 3},    3.5, (1/125, 1/125), (100, 400), "fill light"),
    ("night interior",             "lowlight",  "photo",  4, True,  {},                  3.5, (1/60, 1/60),   (3200, 8000), ""),
    ("dim room portrait, handheld (the owner's room)", "portrait", "photo", 3, True, {}, 3.5, (1/60, 1/60), (6400, 12800), "1/60 instead of 1/125"),
    ("dim room interview, handheld", "interview", "photo", 4, True, {},                  3.5, (1/60, 1/60),   (3200, 8000), "1/60 instead of 1/100"),
    ("dim room product, tripod",   "product",   "photo",  4, False, {},                  5.6, (1/60, 1/60),   (6400, 12800), "tripod"),
    ("bright sun exterior",        "daylight",  "video", 15, True,  {},                  8.0, (1/50, 1/50),   (500, 500), "ND"),
    ("open shade",                 "daylight",  "video", 12, True,  {},                  8.0, (1/50, 1/50),   (500, 500), "ND"),
    ("overcast exterior",          "daylight",  "video", 13, True,  {},                  8.0, (1/50, 1/50),   (500, 500), "ND"),
    ("window-lit interview",       "interview", "video",  9, True,  {},                  4.0, (1/50, 1/50),   (500, 800), ""),
    ("tungsten room (dim)",        "interview", "video",  6, True,  {},                  3.5, (1/50, 1/50),   (1250, 2000), ""),
    ("mixed light room",           "interview", "video",  7, True,  {"highlight": 3},    3.5, (1/50, 1/50),   (640, 1000), ""),
    ("product, table + lamp",      "product",   "video",  8, False, {},                  4.0, (1/50, 1/50),   (500, 640), ""),
    ("backlit subject",            "interview", "video", 10, True,  {"backlight": 3},    4.0, (1/50, 1/50),   (500, 500), ""),
    ("night interior",             "lowlight",  "video",  4, True,  {},                  3.5, (1/50, 1/50),   (6400, 12800), "light"),
]


def run_scenario(monkeypatch, tmp_path, sc):
    name, prof, mode, ev, person, extra, *_ = sc
    for mod in (presets, webui, capture):
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui.assistant, "has_key", lambda: False)
    (tmp_path / "prefs.json").write_text('{"post": true, "wb_kelvin": 5200}')
    cam = ZVE10(ev=ev, person=person, mode=mode, **extra)
    monkeypatch.setattr(measure, "_faces_cv", lambda im: cam.face_box_px(im.width, im.height))
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/preset", json={"preset": prof, "mode": mode}).json()
    p = cam.p
    return {"cam": cam, "verdict": r.get("verdict") or {}, "f": p[PROPS["aperture"]].value / 100,
            "t": presets.shutter_seconds(p[PROPS["shutter_speed"]].value), "iso": p[PROPS["iso"]].value,
            "stops": cam.stops(), "focus": p[PROPS["focus_mode"]].value, "pp": p[presets.PROP_PICTURE_PROFILE].value,
            "mode": cam.mode}


def _fmt_t(t):
    return f"1/{round(1 / t)}" if t < 1 else f"{t:g}s"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[f"{s[2]}-{s[0]}" for s in SCENARIOS])
def test_professional_decision(monkeypatch, tmp_path, sc):
    name, prof, mode, ev, person, extra, f_exp, t_rng, iso_rng, advice = sc
    r = run_scenario(monkeypatch, tmp_path, sc)
    txt = " ".join(i["text"] + " " + i["todo"] for i in r["verdict"].get("items", []))
    got = f"f/{r['f']:g} {_fmt_t(r['t'])} ISO {r['iso']}"
    assert r["mode"] == mode and (r["pp"] == 8) == (mode == "video"), got
    assert abs(math.log2(r["f"] / f_exp)) <= 0.2, f"aperture {got}, expected f/{f_exp:g}"
    lo, hi = t_rng
    assert lo * 0.93 <= r["t"] <= hi * 1.07, f"shutter {got}, expected {_fmt_t(lo)}..{_fmt_t(hi)}"
    assert iso_rng[0] <= r["iso"] <= iso_rng[1], f"ISO {got}, expected {iso_rng}"
    assert r["iso"] <= capture.ISO_CEILING[mode]
    assert r["focus"] == 1, "focus must end locked (MF)"
    assert not r["cam"].refused, f"refused writes: {r['cam'].refused}"
    if advice:
        assert advice.lower() in txt.lower(), f"expected advice '{advice}' in: {txt}"
    if mode == "video":
        assert abs(r["t"] - 1 / 50) < 1e-6, "video always keeps 1/50 at 25p"
    # no contradictions in what it tells him
    assert not ("Over-exposed" in txt and "not enough light" in txt), txt


def test_iso_is_the_lowest_that_exposes(monkeypatch, tmp_path):
    """Lowest ISO: one third stop less would be under by more than the tolerance (photo, window interview)."""
    r = run_scenario(monkeypatch, tmp_path, SCENARIOS[3])
    cam = r["cam"]
    isos = sorted(v for v in cam.p[PROPS["iso"]].choices if v != 0xFFFFFF)
    i = isos.index(r["iso"])
    assert i == 0 or abs(math.log2(isos[i - 1] / r["iso"])) <= 0.34
