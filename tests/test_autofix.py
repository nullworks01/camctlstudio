"""OPERATOR: decide, lock and hold; proposals only on real changes; nothing during a take; base ISO first."""
import io

from PIL import Image, ImageDraw
from starlette.testclient import TestClient

from camctl import autofix, capture, presets, scene, webui
from camctl.drivers.sony import PROPS, PropInfo

from test_autopilot import StubCam, _studio_with


class _St:
    def __init__(self):
        self.cam, self.frame, self.recording, self.last_view_req = object(), b"x", False, 0


def _op(monkeypatch, ire_seq, prefs=None):
    monkeypatch.setattr(autofix.AutoFix, "_covered", staticmethod(lambda frame: False))   # these tests feed placeholder frames
    for k in ("HOLD_S", "LIGHT_HOLD_S", "SHOT_HOLD_S", "LEAVE_HOLD_S", "SETTLE_S", "COOLDOWN_S", "PROPOSAL_GAP_S", "TOUCH_QUIET_S"):
        monkeypatch.setattr(autofix, k, 0.0)
    monkeypatch.setattr(autofix, "_last_setup_time", lambda: 0.0)
    monkeypatch.setattr(autofix, "_mark_setup_time", lambda: None)
    st = _St()
    it = iter(ire_seq)
    decided, applied = [], []
    sc = lambda f: {"subject": {"type": "person", "ire": next(it), "box": [0.4, 0.2, 0.2, 0.3], "size": "medium"},
                    "light_colour": {"face": 0.05}, "key": {"side": "left", "ratio": 2, "ratio_text": "2:1"},
                    "overall_ire": 40, "clip_pct": 0, "log": True, "background": {"separation_stops": 1}, "flicker": 0,
                    "clip_zones": [], "backlit": False}
    op = autofix.AutoFix(st, None, None, lambda: {"exposure_mode": 0x78053}, scene_fn=sc,
                         decide_fn=lambda f, s, m, c: decided.append(m) or {"shot": "interview", "shot_name": "talking head",
                                                                             "why": ["x"], "directions": [], "entry": None},
                         apply_fn=lambda p: applied.append(p) or ["iso"], prefs=lambda: prefs or {"auto_apply": False})
    return op, st, decided, applied


def test_decides_once_then_holds_and_proposes_only_on_real_change(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50, 50, 50, 48, 52, 50, 30, 30, 30])
    op.tick()                                       # connect -> decide + lock
    assert decided == ["video"] and op.phase == "locked"
    for _ in range(5):                              # small wobbles: nothing
        op.tick()
    assert op.proposal is None and not applied
    op.tick()                                       # light dropped ~1.5 stops
    assert op.proposal and op.proposal["kind"] == "light" and "dropped" in op.proposal["text"]
    assert op.phase == "proposal" and not applied   # auto-apply off: only proposed


def test_nothing_changes_during_a_take_and_applies_between_takes(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50, 50, 30, 30, 30, 30, 30], prefs={"auto_apply": True})
    op.tick()
    op.tick()
    st.recording = True
    for _ in range(3):
        op.tick()                                   # during the take: no watching, no applying
    assert not applied and op.proposal is None
    st.recording = False
    op.tick()                                       # between takes: proposal made and applied automatically
    assert applied and applied[0]["kind"] == "light"
    op2, st2, _d, applied2 = _op(monkeypatch, [50] * 10, prefs={"auto_apply": True})
    op2.tick()
    op2.proposal = {"id": 9, "kind": "light", "text": "t", "stops": 1}
    st2.recording = True
    assert op2.apply_proposal(9) == [] and not applied2            # refused mid-take by default


def test_scene_decisions_for_typical_cases():
    base = {"overall_ire": 45, "clip_pct": 0, "log": True, "flicker": 0, "clip_zones": [],
            "light_colour": {"face": 0.05, "background": 0.04, "mixed": False}, "moving": False}
    person = {"type": "person", "ire": 50, "size": "medium", "box": [0.4, 0.2, 0.2, 0.3]}
    k2 = {"side": "left", "ratio": 2, "ratio_text": "2:1"}
    backlit = scene.decide({**base, "subject": person, "backlit": True, "key": k2, "background": {"separation_stops": 1}}, "video")
    assert backlit["shot"] == "interview" and any(d["key"] == "backlight" for d in backlit["directions"])
    mixed = scene.decide({**base, "subject": person, "backlit": False, "key": k2, "background": {"separation_stops": 1},
                          "light_colour": {"face": 0.2, "background": -0.05, "mixed": True}}, "video")
    assert any(d["key"] == "mixed" for d in mixed["directions"])
    dark = scene.decide({**base, "subject": {**person, "ire": 15}, "backlit": False, "key": k2,
                         "background": {"separation_stops": 1}}, "video")
    assert dark["shot"] == "lowlight" and any(d["key"] == "dark" for d in dark["directions"])
    ext = scene.decide({**base, "overall_ire": 85, "clip_pct": 12, "subject": {"type": "scene", "ire": 80, "size": "wide", "box": None},
                        "backlit": False, "key": {"side": "left", "ratio": 1, "ratio_text": "1:1"}, "background": None}, "video")
    assert ext["shot"] == "daylight"
    prod = scene.decide({**base, "subject": {"type": "object", "ire": 50, "size": "close", "box": [0.3, 0.25, 0.4, 0.5]},
                         "backlit": False, "key": k2, "background": None}, "photo")
    assert prod["shot"] == "product"
    fl = scene.decide({**base, "flicker": 0.6, "subject": person, "backlit": False, "key": k2,
                       "background": {"separation_stops": 1}}, "video")
    assert any("flicker" in w.lower() for w in fl["why"])


def _jpeg(im):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=95)
    return b.getvalue()


def test_scene_analysis_detects_product_and_flicker_bands():
    im = Image.new("RGB", (320, 180), (120, 110, 100))
    d = ImageDraw.Draw(im)
    for x in range(110, 210, 6):                    # a detailed object in the middle
        d.line((x, 50, x + 3, 130), fill=(20, 20, 20), width=2)
    s = scene.analyze(_jpeg(im), log=False)
    assert s["subject"]["type"] == "object"
    bands = Image.new("RGB", (320, 180), (100, 100, 100))
    d = ImageDraw.Draw(bands)
    for y in range(0, 180, 12):
        d.rectangle((0, y, 320, y + 5), fill=(140, 140, 140))
    assert scene.banding(bands) > 0.3 and scene.banding(Image.new("RGB", (320, 180), (100, 100, 100))) == 0.0


def test_profile_uses_base_iso_manual_mode_and_locked_wb(monkeypatch, tmp_path):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam = StubCam(scene_ev=6)                       # plenty of light
    cam.p[PROPS["iso"]].value = 6400
    cam.p[0xD20F] = PropInfo(0xD20F, 4, True, True, 5600, range=(2500, 9900, 100))
    (tmp_path / "prefs.json").write_text('{"post": true, "wb_kelvin": 4300}')
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/preset", json={"preset": "interview", "mode": "photo"}).json()
    isos = sorted(v for v in cam.p[PROPS["iso"]].choices if 0 < v < 0xFFFFFF)
    assert cam.p[PROPS["iso"]].value == isos[0]                     # base ISO: the light is enough
    assert cam.p[PROPS["exposure_mode"]].value == capture.PHOTO_M   # full manual
    assert cam.p[PROPS["white_balance"]].value == capture.WB_KELVIN and cam.p[0xD20F].value == 4300
    assert next(i for i in r["verdict"]["items"] if i["key"] == "wb")["status"] == "ok"


def test_light_proposal_prefers_iris_before_iso(monkeypatch, tmp_path):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    cam = StubCam()
    cam.p[PROPS["exposure_mode"]].value = 0x78053
    cam.p[PROPS["aperture"]].value = 560
    cam.p[PROPS["iso"]].value = 800
    app = webui.build_app(_studio_with(cam))
    import gc
    op = next(o for o in gc.get_objects() if isinstance(o, autofix.AutoFix) and getattr(o.studio, "cam", None) is cam)
    with TestClient(app) as c:
        op.decision = {"shot": "interview"}
        op.proposal = {"id": 1, "kind": "light", "text": "The light dropped 1 stop.", "stops": 1.0}
        c.post("/api/autofix", json={"apply": 1})
    assert cam.p[PROPS["aperture"]].value < 560 and cam.p[PROPS["iso"]].value == 800


def test_manual_exposure_change_is_not_taken_for_a_light_change(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50, 50, 25, 25, 25, 25])
    op.tick()
    op.tick()
    op.manual("iso")                               # the owner darkened the picture himself
    op.tick()                                      # first frame after: new reference (25), no proposal
    op.tick()
    assert op.proposal is None and op.baseline["ire"] == 25


def test_subject_change_is_proposed(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50] * 20)
    op.tick()
    op.tick()
    orig = op.scene_fn
    op.scene_fn = lambda f: {**orig(f), "subject": {"type": "object", "ire": 50, "box": [0.3, 0.25, 0.4, 0.5], "size": "close"}}
    for _ in range(6):                             # the same new subject in every recent reading
        op.tick()
    assert op.proposal and op.proposal["kind"] == "shot" and "object" in op.proposal["text"]


def test_focus_proposal_on_soft_focus_or_distance_change(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50] * 30)
    op.tick()
    base = op.scene_fn
    op.scene_fn = lambda f: {**base(f), "subject": {"type": "person", "ire": 50, "box": [0.4, 0.2, 0.2, 0.3], "size": "medium", "sharp": 1.2}}
    op.tick()                                     # reference: sharp 1.2, size 0.3
    op.tick()
    assert op.proposal is None
    op.scene_fn = lambda f: {**base(f), "subject": {"type": "person", "ire": 50, "box": [0.35, 0.1, 0.3, 0.45], "size": "close-up", "sharp": 1.2}}
    op.tick()
    assert op.proposal and op.proposal["kind"] == "focus" and "closer" in op.proposal["text"]
    op.manual("focus_mode")                       # he refocused himself: proposal gone, new reference
    assert op.proposal is None and op.baseline["size"] is None


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t


def _real_op(monkeypatch, scene_seq, prefs):
    """Real timing constants on a fake clock; counts every camera write the Operator could make."""
    clock = _Clock()
    monkeypatch.setattr(autofix, "time", clock)
    monkeypatch.setattr(autofix, "_last_setup_time", lambda: 0.0)
    monkeypatch.setattr(autofix, "_mark_setup_time", lambda: None)
    st = _St()
    it = iter(scene_seq)
    writes = []
    base = {"light_colour": {"face": 0.05}, "key": {"side": "left", "ratio": 2, "ratio_text": "2:1"},
            "overall_ire": 40, "clip_pct": 0, "log": True, "background": {"separation_stops": 1}, "flicker": 0,
            "clip_zones": [], "backlit": False}
    op = autofix.AutoFix(st, None, None, lambda: {"exposure_mode": 0x78053}, scene_fn=lambda f: {**base, "subject": next(it)},
                         decide_fn=lambda f, s, m, c: writes.append("setup") or {"shot": "interview", "shot_name": "talking head",
                                                                                "why": [], "directions": [], "entry": None},
                         apply_fn=lambda p: writes.append(p["kind"]) or ["iso"], prefs=lambda: prefs)
    return op, clock, writes


def _person(ire):
    return {"type": "person", "ire": ire, "box": [0.4, 0.2, 0.2, 0.3], "size": "medium", "sharp": 1.0}


def _scene(ire):
    return {"type": "scene", "ire": ire, "box": None, "size": "wide"}


def test_face_lost_and_found_with_flickering_light_never_writes_after_first_setup(monkeypatch):
    import math as _m
    seq = []
    for i in range(400):                           # 10 minutes: looks down for 40 s at a time, brightness wobbles
        ire = 45 + 7 * _m.sin(i / 1.3)             # lamp flicker / auto-exposure wobble, about +-0.4 stop; frame-wide reading darker
        seq.append(_scene(ire * 0.6) if (i // 27) % 2 else _person(ire))
    for prefs in ({}, {"auto_apply": True}):        # default (proposals only) and even with auto-apply on
        op, clock, writes = _real_op(monkeypatch, seq + [_person(45)] * 50, prefs)
        op.tick()                                   # first setup on connect
        assert writes == ["setup"]
        for _ in range(399):
            clock.t += 1.5
            op.tick()
        assert writes == ["setup"], writes          # zero camera writes after the first setup
        assert op.decision.get("shot") == "interview" and op.baseline.get("subject") == "person"
        assert not any(e["kind"] == "proposal" and "shot" in e["text"].lower() for e in op.events)


def test_proposals_are_rate_limited_and_not_applied_by_default(monkeypatch):
    seq = [_person(50)] * 10 + [_person(20)] * 400       # the light really drops and stays down
    op, clock, writes = _real_op(monkeypatch, seq, {})
    op.tick()
    for _ in range(300):
        clock.t += 1.5
        op.tick()
        if op.proposal:
            op.dismiss()                                  # he says no: no chain of new proposals
    props = [e for e in op.events if e["kind"] == "proposal"]
    assert writes == ["setup"]                            # default: proposals only
    assert 1 <= len(props) <= 3                           # 450 s: at most one per few minutes
    gaps = [b["t"] - a["t"] for a, b in zip(props, props[1:])]
    assert all(g >= autofix.PROPOSAL_GAP_S for g in gaps)


def test_no_proposal_while_he_works_and_body_changes_are_reported(monkeypatch):
    seq = [_person(50)] * 10 + [_person(20)] * 200
    vals = {"iso": "ISO 2500", "aperture": "f/3.5", "exposure_mode": "M"}
    op, clock, writes = _real_op(monkeypatch, seq, {})
    op.labels_fn = lambda: dict(vals)
    op.tick()
    clock.t += 30
    op.tick()                                             # first look at the settings
    vals.update(iso="ISO 100", aperture="f/8")            # turned on the camera body
    clock.t += 1.5
    op.tick()
    ev = [e for e in op.events if e["kind"] == "camera"]
    assert ev and "f/8" in ev[-1]["text"] and "ISO 100" in ev[-1]["text"]
    assert writes == ["setup"]                            # reported, not silently corrected
    op.manual("iso")
    vals.update(iso="ISO 800")
    clock.t += 1.5
    op.tick()
    assert any(e["kind"] == "you" and "ISO 800" in e["text"] for e in op.events)
    for _ in range(20):                                   # 30 s after his change: no proposal yet
        clock.t += 1.5
        op.tick()
    assert op.proposal is None


def test_switching_mode_is_a_full_fresh_setup_and_ends_locked(monkeypatch, tmp_path):
    import gc
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui.setups, "BASE", tmp_path)
    monkeypatch.setattr(webui.assistant, "has_key", lambda: False)
    cam = StubCam(scene_ev=1)
    cam.p[PROPS["exposure_mode"]].value = 0x78053            # he just switched the camera to Movie M
    cam.p[PROPS["focus_mode"]].value = 32772                  # the movie memory: AF-C ...
    cam.p[0xD22C] = PropInfo(0xD22C, 4, True, True, 1, choices=[1, 2, 259, 514])   # ... Wide
    cam.p[PROPS["iso"]].value = 100
    (tmp_path / "prefs.json").write_text('{"post": true}')
    st = _studio_with(cam)
    app = webui.build_app(st)
    op = next(o for o in gc.get_objects() if isinstance(o, autofix.AutoFix) and getattr(o.studio, "cam", None) is cam)
    with TestClient(app) as c:
        st.latest_frame(-1, timeout=3)
        r = c.post("/api/post", json={"post": True, "mode": "video"}).json()
        assert r["phase"] == "locked" and r["verdict"]["items"]
        assert cam.p[presets.PROP_PICTURE_PROFILE].value == 8                  # S-Log3
        assert cam.p[PROPS["focus_mode"]].value == 1 and cam.p[0xD22C].value != 1   # acquired once, locked; not AF-C Wide
        assert presets.shutter_seconds(cam.p[PROPS["shutter_speed"]].value) == 1 / 50   # 25p, 50 Hz mains
        assert op.baseline["ire"] is None and op.baseline["mode"] == "video"           # fresh reference
        assert any(e["kind"] == "you" and "switched to video" in e["text"] for e in op.events)
        assert any(e["kind"] == "setup" and "set up for video" in e["text"] for e in op.events)
        op._quiet_until = 0
        for _ in range(4):
            op.tick()
        assert op.proposal is None                                                # no stale reference -> no proposal
        cam.p[PROPS["exposure_mode"]].value = capture.PHOTO_M
        cam.p[PROPS["focus_mode"]].value = 32772
        r = c.post("/api/post", json={"post": True, "mode": "photo"}).json()
        assert r["phase"] == "locked" and cam.p[PROPS["focus_mode"]].value == 1
        assert op.baseline["mode"] == "photo"
        # video -> photo: the normal picture (log from video not carried over) unless "Match video" is on
        assert cam.p[presets.PROP_PICTURE_PROFILE].value == 0
        assert any("Preview: camera standard" in i["text"] for i in r["verdict"]["items"])
        c.post("/api/post", json={"photo_log": True})
        r = c.post("/api/post", json={"post": True, "mode": "photo"}).json()
        assert cam.p[presets.PROP_PICTURE_PROFILE].value == 8 and r["phase"] == "locked"
        assert any("Preview: S-Log3" in i["text"] for i in r["verdict"]["items"])


def test_lights_off_never_starts_a_setup_or_writes_anything(monkeypatch):
    """the owner's rule: after the first setup nothing changes by itself. Even the room going dark (5 stops) only
    raises a one-tap suggestion; no new setup, no write."""
    op, st, decided, applied = _op(monkeypatch, [50, 50, 50, 8, 8, 8, 8, 8])
    op.tick()                                       # first connect: the one setup
    for _ in range(7):
        op.tick()
    assert decided == ["video"] and not applied
    assert op.proposal and op.proposal["kind"] == "light"


def test_a_reconnect_with_the_camera_as_set_up_does_not_set_up_again(monkeypatch):
    op, st, decided, applied = _op(monkeypatch, [50] * 20)
    monkeypatch.setattr(autofix, "_last_setup_time", lambda: autofix.time.time() - 3600)   # an hour ago: not "recent"
    monkeypatch.setattr(autofix, "_same_as_last_setup", lambda v: True)                    # camera exactly as set up
    op.tick()
    assert decided == [] and op.phase == "locked"                                        # a restart is not a first connect
    st.cam = None
    op.tick()                                       # Wi-Fi blip
    st.cam = object()
    op.tick()
    assert decided == [] and not applied


# -- 12 Nov 2026: a 6.8-stop spike while he fitted an ND became a proposal he applied, closing to f/6.3 ISO 100 -------
def _op_clock(monkeypatch, ire_seq, prefs=None):
    """Like _op, but the light hold is real (20 s) on a controllable clock."""
    op, st, decided, applied = _op(monkeypatch, ire_seq, prefs)
    monkeypatch.setattr(autofix, "LIGHT_HOLD_S", 20.0)
    now = [1000.0]

    class _Clock:
        @staticmethod
        def time():
            return now[0]
    monkeypatch.setattr(autofix, "time", _Clock)
    return op, decided, applied, now


def test_a_short_spike_never_raises_a_proposal(monkeypatch):
    seq = [50, 50] + [95] * 4 + [50] * 10          # 8 s of a much brighter picture (filter being fitted), then back
    op, decided, applied, now = _op_clock(monkeypatch, seq)
    op.tick()
    for _ in range(15):
        now[0] += 2.0
        op.tick()
    assert op.proposal is None and not applied


def test_a_steady_change_shows_the_values_before_he_presses(monkeypatch):
    op, decided, applied, now = _op_clock(monkeypatch, [50, 50] + [20] * 20)
    op.plan_text = lambda p: "f/3.5 · ISO 6400"
    op.tick()
    for _ in range(14):
        now[0] += 2.0
        op.tick()
    assert op.proposal and "Apply sets f/3.5 · ISO 6400." in op.proposal["text"]
    assert not applied                               # auto-apply off: never applied without his press


def test_auto_apply_off_never_applies_anything(monkeypatch):
    op, decided, applied, now = _op_clock(monkeypatch, [50, 50] + [20] * 60, prefs={"auto_apply": False})
    op.tick()
    for _ in range(59):
        now[0] += 5.0
        op.tick()
    assert op.proposal and not applied


def test_a_covered_lens_is_not_measured(monkeypatch):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (320, 180), (3, 3, 3)).save(b, "JPEG")
    assert autofix.AutoFix._covered.__func__(b.getvalue()) if hasattr(autofix.AutoFix._covered, "__func__") else True
    from camctl.autofix import AutoFix
    real = AutoFix.__dict__["_covered"]
    monkeypatch.undo()                               # the real check, not the placeholder one
    assert AutoFix._covered(b.getvalue()) is True    # lens cap / hand: a black, flat picture
    b2 = io.BytesIO()
    im = Image.new("RGB", (320, 180), (120, 110, 100))
    for x in range(0, 320, 20):
        im.paste((220, 220, 220), (x, 40, x + 8, 140))
    im.save(b2, "JPEG")
    assert AutoFix._covered(b2.getvalue()) is False  # a normal picture with detail


# -- the camera back within the working day: unchanged = nothing; different = say it, change nothing, no Claude ----
def _reconnect(monkeypatch, last, now_vals, age_s):
    monkeypatch.setattr(autofix.AutoFix, "_covered", staticmethod(lambda frame: False))
    monkeypatch.setattr(autofix, "_last_setup_time", lambda: autofix.time.time() - age_s)
    monkeypatch.setattr(autofix, "_mark_setup_time", lambda: None)
    monkeypatch.setattr(autofix, "_last_setup_values", lambda: last)
    monkeypatch.setattr(autofix, "_same_as_last_setup", lambda v: all(last.get(k) == v.get(k) for k in last))
    st = _St()
    decided, claude = [], []
    sc = lambda f: {"subject": {"type": "person", "ire": 50, "box": [0.4, 0.2, 0.2, 0.3]}, "light_colour": {}}
    op = autofix.AutoFix(st, None, None, lambda: dict(now_vals), scene_fn=sc,
                         decide_fn=lambda f, s, m, c: (decided.append(m), claude.append(c)) and {} or
                         {"shot": "portrait", "shot_name": "portrait", "why": [], "directions": [], "entry": None},
                         prefs=lambda: {"auto_apply": False}, has_key=lambda: True)
    op.tick()
    return op, decided, claude


VALS = {"exposure_mode": 1, "aperture": 350, "shutter_speed": 65596, "iso": 2500, "white_balance": 32786,
        "color_temp": 3000, "focus_mode": 1, "picture_profile": 0}


def test_back_unchanged_does_nothing(monkeypatch):
    op, decided, claude = _reconnect(monkeypatch, VALS, VALS, 3 * 3600)
    assert decided == [] and op.phase == "locked"


def test_back_with_different_values_says_it_and_changes_nothing(monkeypatch):
    op, decided, claude = _reconnect(monkeypatch, VALS, {**VALS, "iso": 100, "focus_mode": 2}, 3 * 3600)
    assert decided == [] and claude == []                                  # no setup, no Claude call
    assert op.phase == "changed" and op.state.startswith("The camera came back with different settings (ISO and focus)")
    assert any("ISO 2500 → 100" in e["text"] and "Nothing was changed" in e["text"] for e in op.events)


def test_true_first_connect_sets_up_once(monkeypatch):
    op, decided, claude = _reconnect(monkeypatch, {}, VALS, 10 * 3600)
    assert len(decided) == 1 and claude == [True]


def test_a_photo_video_setup_counts_as_the_last_setup(monkeypatch, tmp_path):
    """12 Nov 2026: after a Photo/Video setup a restart said 'the camera came back with different settings'
    because only the first-connect setup saved its values."""
    from starlette.testclient import TestClient
    from camctl import handoff, setups, webui
    from camctl.drivers import sony
    from zve10_sim import ZVE10
    from test_autopilot import _studio_with
    monkeypatch.setattr(sony, "S1_SETTLE_S", 0.0)
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(webui, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    cam = ZVE10(ev=10, mode="photo")
    st = _studio_with(cam)
    with TestClient(webui.build_app(st)) as c:
        st.latest_frame(-1, timeout=3)
        c.post("/api/set", json={"name": "exposure_mode", "value": 0x78053})
        c.post("/api/post", json={"post": True, "mode": "video", "setup": True})
        now = {s["name"]: s["value"] for s in c.get("/api/settings").json()["settings"]}
    saved = autofix._last_setup_values()
    assert saved and all(saved[k] == now[k] for k in ("exposure_mode", "iso", "aperture", "shutter_speed") if k in saved)
