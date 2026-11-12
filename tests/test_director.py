"""DIRECTOR tool loop, change log with undo, and Lighting Lab endpoints (no camera, no API)."""
import time

from starlette.testclient import TestClient

from camctl import assistant, director, presets, webui
from camctl.drivers.sony import PROPS

from test_autopilot import StubCam, _studio_with


def _wait_idle(c, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/director/status").json()
        if not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("director did not finish")


def test_director_loop_changes_logs_and_undoes(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "test")
    calls = []

    def fake_call(body, key):
        calls.append(body)
        tool_names = {t["name"] for t in body["tools"]}
        assert "shoot" not in tool_names and "record" not in tool_names   # never a shutter tool
        n = len(calls)
        if n == 1:   # first turn: an invalid value, a valid ISO change and a look
            return {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Lowering ISO."},
                {"type": "tool_use", "id": "a", "name": "set_setting", "input": {"name": "iso", "value": 999, "reason": "x"}},
                {"type": "tool_use", "id": "b", "name": "set_setting", "input": {"name": "iso", "value": 400, "reason": "Less noise."}},
                {"type": "tool_use", "id": "c", "name": "look", "input": {}}]}
        results = body["messages"][-1]["content"]
        assert results[0]["is_error"] and "not allowed" in results[0]["content"]
        assert results[2]["content"][0]["type"] == "image"
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "d", "name": "finish", "input": {"summary": "Set.", "advice": "Add a key light."}}]}

    monkeypatch.setattr(assistant, "call", fake_call)
    cam = StubCam()
    app = webui.build_app(_studio_with(cam))
    with TestClient(app) as c:
        assert c.post("/api/director/start", json={"goal": "clean", "mode": "photo"}).json()["ok"]
        s = _wait_idle(c)
        assert s["state"] == "done" and s["summary"] == "Set." and s["advice"] == "Add a key light."
        assert cam.p[PROPS["iso"]].value == 400
        change = next(e for e in s["log"] if e["kind"] == "change")
        assert change["undoable"] and "ISO" in change["text"].upper() or "iso" in change["text"]
        r = c.post("/api/director/undo", json={"run": s["run"]}).json()
        assert r["ok"]
        assert cam.p[PROPS["iso"]].value == 0xFFFFFF          # back to auto ISO
        s = c.get("/api/director/status").json()
        assert not next(e for e in s["log"] if e["kind"] == "change")["undoable"]


def test_director_needs_key_and_stops(monkeypatch):
    monkeypatch.setattr(assistant, "has_key", lambda: False)
    app = webui.build_app(_studio_with(StubCam()))
    with TestClient(app) as c:
        r = c.post("/api/director/start", json={"goal": ""})
        assert r.status_code == 400 and r.json()["error"] == "NO_KEY"
    monkeypatch.setattr(assistant, "has_key", lambda: True)
    monkeypatch.setattr(assistant, "_key", lambda: "test")

    def slow_call(body, key):
        time.sleep(0.3)
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "x", "name": "look", "input": {}}]}
    monkeypatch.setattr(assistant, "call", slow_call)
    app = webui.build_app(_studio_with(StubCam()))
    with TestClient(app) as c:
        c.post("/api/director/start", json={"goal": "loop"})
        time.sleep(0.1)
        c.post("/api/director/stop")
        s = _wait_idle(c)
        assert s["state"] == "stopped" and s["step"] < director.MAX_STEPS


def test_lighting_plan_validates_camera_settings(monkeypatch):
    monkeypatch.setattr(assistant, "_key", lambda: "test")
    plan = {"title": "Rembrandt", "look_analysis": "", "key_fill_ratio": "4:1", "contrast": "low-key",
            "lights": [{"role": "key", "fixture": "COB", "modifier": "softbox", "angle_deg": 405, "distance_m": 1.5,
                        "height_m": 2.1, "power_stops": 0, "color_temp_k": 5600, "gel": "none", "note": ""}],
            "background": "", "practicals": "", "steps": ["Place key"],
            "camera_settings": [{"name": "iso", "value": 400}, {"name": "iso", "value": 123}], "camera_notes": ""}
    import json
    monkeypatch.setattr(assistant, "call", lambda body, key: {"stop_reason": "end_turn",
                                                              "content": [{"type": "text", "text": json.dumps(plan)}]})
    cam = StubCam()
    app = webui.build_app(_studio_with(cam))
    with TestClient(app) as c:
        j = c.post("/api/lighting/plan", json={"refs": ["data:image/jpeg;base64,/9j/AA=="], "include_live": True}).json()
        assert j["lights"][0]["angle_deg"] == 45
        assert [x["value"] for x in j["camera"]] == [400] and j["dropped"] == ["iso=123"]
        r = c.post("/api/changes/apply", json={"changes": [{"name": "iso", "value": 400}], "source": "lighting"}).json()
        assert r["applied"] == ["iso"] and cam.p[PROPS["iso"]].value == 400
        assert c.post("/api/lighting/compare", json={"refs": []}).status_code == 400


def test_measure_finds_skin_and_zones(monkeypatch):
    import io
    from camctl import measure as _m
    monkeypatch.setattr(_m, "_faces_cv", lambda im: None)     # the no-OpenCV fallback
    from PIL import Image, ImageDraw
    from camctl import measure
    im = Image.new("RGB", (320, 180), (40, 40, 40))
    ImageDraw.Draw(im).rectangle((130, 40, 190, 120), fill=(224, 172, 140))     # a face-coloured patch
    ImageDraw.Draw(im).rectangle((0, 0, 40, 30), fill=(255, 255, 255))           # a clipped window
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=95)
    m = measure.analyze(buf.getvalue())
    f = m["face"]
    assert f["method"] == "skin-colour heuristic" and 60 < f["mean"] < 80
    assert 0.35 < f["box"][0] < 0.45 and m["zones"]["top-left"]["clip_pct"] > 5
    assert m["false_colour_pct"]["clipped (>=98)"] > 1
    r = measure.region_stats(buf.getvalue(), 0, 0, 0.1, 0.1)
    assert r["clip_pct"] > 90
    assert "S-Log3" in measure.summary(m, log=True)


def test_director_never_sees_wb_shift_and_undo_covers_its_changes(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    cam = StubCam()
    st = _studio_with(cam)
    view = webui.director_settings_view(st.props())
    assert not any(s["name"].startswith("wb_shift") for s in view)          # colour stays neutral
    log = director.ChangeLog(st, webui.director_settings_view, webui.director_code_of, webui.CONTROLS)
    assert log.apply([{"name": "wb_shift_ab", "value": 192}], "director")["failed"]
    cam.p[PROPS["iso"]].value = 400
    r = log.apply([{"name": "iso", "value": 800}], "director")
    assert r["applied"] == ["iso"]
    assert not log.undo([log.entries[-1]["id"]]) and cam.p[PROPS["iso"]].value == 400
def test_setups_save_recall_undo_delete_and_gear(monkeypatch, tmp_path):
    from camctl import setups
    monkeypatch.setattr(setups, "BASE", tmp_path)
    monkeypatch.setattr(setups, "SETUP_DIR", tmp_path / "setups")
    monkeypatch.setattr(setups, "GEAR_FILE", tmp_path / "gear.json")
    seen = {}
    monkeypatch.setattr(assistant, "lighting_plan", lambda *a, **k: seen.update(k) or {"lights": [], "camera": [], "dropped": []})
    cam = StubCam()
    app = webui.build_app(_studio_with(cam))
    with TestClient(app) as c:
        cam.p[PROPS["iso"]].value = 400
        sid = c.post("/api/setups/save", json={"name": "Interview A", "notes": "desk", "mode": "video",
                                               "preset": "interview", "plan": {"title": "P", "lights": []}}).json()["id"]
        lst = c.get("/api/setups").json()
        assert lst[0]["name"] == "Interview A" and lst[0]["thumb"] and lst[0]["has_plan"] and lst[0]["key"]["iso"] == "400"
        cam.p[PROPS["iso"]].value = 3200
        r = c.post(f"/api/setups/{sid}/recall").json()
        assert "iso" in r["applied"] and cam.p[PROPS["iso"]].value == 400 and r["plan"]["title"] == "P"
        s = c.get("/api/director/status").json()
        e = next(x for x in s["log"] if x["source"] == "setup")
        assert e["undoable"] and "Interview A" in e["reason"]
        c.post("/api/director/undo", json={"ids": [e["id"]]})
        assert cam.p[PROPS["iso"]].value == 3200
        assert c.post(f"/api/setups/{sid}/delete").json()["ok"] and c.get("/api/setups").json() == []
        assert c.get("/api/setups/../../x").status_code == 404
        c.post("/api/gear", json={"gear": "1x COB + softbox"})
        assert c.get("/api/gear").json()["gear"] == "1x COB + softbox"
        c.post("/api/lighting/plan", json={"refs": [], "include_live": True})
        assert seen["gear"] == "1x COB + softbox"


def test_coach_checks_and_exposure_fix(monkeypatch):
    import io
    from camctl import measure as _m
    monkeypatch.setattr(_m, "_faces_cv", lambda im: None)     # synthetic face: use the skin fallback
    from PIL import Image, ImageDraw
    from camctl import coach
    im = Image.new("RGB", (320, 180), (30, 30, 30))
    ImageDraw.Draw(im).rectangle((130, 40, 190, 120), fill=(90, 66, 54))      # a dark face
    ImageDraw.Draw(im).rectangle((0, 0, 60, 40), fill=(255, 255, 255))        # a blown window
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=95)
    r = coach.analyze(buf.getvalue(), {"picture_profile": 0, "iso": 12800}, {"battery": 8})
    by = {c["key"]: c for c in r["checks"]}
    assert by["exposure"]["status"] == "bad" and by["exposure"]["stops"] > 1 and by["exposure"]["fix"]["kind"] == "exposure"
    assert r["overlays"]["subject_is_face"] and r["overlays"]["clip_cells"]
    assert by["noise"]["status"] == "warn" and by["battery"]["status"] == "bad"
    assert r["line"].endswith(".")
    cam = StubCam()
    cam.p[PROPS["iso"]].value = 400
    plan = coach.plan_exposure_fix(cam.refresh(), 2.0, max_iso=6400)
    assert plan and plan[0] == ("iso", 1600)


def test_simple_auto_builtin_logs_undoable_and_coach_endpoint(monkeypatch):
    monkeypatch.setattr(presets.time, "sleep", lambda s: None)
    monkeypatch.setattr(assistant, "has_key", lambda: False)
    cam = StubCam(scene_ev=-2)
    st = _studio_with(cam)
    app = webui.build_app(st)
    with TestClient(app) as c:
        before = cam.p[PROPS["shutter_speed"]].value, cam.p[PROPS["iso"]].value
        j = c.post("/api/simple/auto", json={"mode": "photo", "preset": "portrait"}).json()
        assert j["via"] == "auto" and j["preset"] == "portrait"
        s = c.get("/api/director/status").json()
        e = next(x for x in s["log"] if x["id"] == j["entry"])
        assert e["undoable"] and "Portrait" in e["text"]
        c.post("/api/director/undo", json={"ids": [j["entry"]]})
        assert (cam.p[PROPS["shutter_speed"]].value, cam.p[PROPS["iso"]].value) == before
        st.latest_frame(-1, timeout=3)
        k = c.get("/api/coach").json()
        assert {x["key"] for x in k["checks"]} >= {"exposure", "focus", "noise"} and k["line"]
        r = c.post("/api/coach/fix", json={"kind": "exposure", "stops": 1.0}).json()
        assert "applied" in r
