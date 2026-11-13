"""camctl Studio on a SIMULATED ZV-E10 (port 8771), for testing the whole UI without the camera.

    python tools/sim_studio.py [--ev 8] [--video]

Nothing here touches the real camera. Photos/recordings are simulated (no files on a card).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# The simulator never touches his real state: every file Studio keeps under %APPDATA%\camctl (operator log, setup
# marker, last-setup values, camera profiles, usage, capture log, prefs, Wi-Fi settings, key) goes to a temp folder.
# This must happen before camctl is imported (some paths are fixed at import).
SIM_HOME = Path(tempfile.mkdtemp(prefix="camctl_sim_home_"))
REAL_APPDATA = os.environ.get("APPDATA", "")
os.environ["APPDATA"] = str(SIM_HOME)

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
# the libraries Studio installs "for this Windows user" live under the real profile's Roaming folder; with APPDATA
# pointed at a temp folder Python would not find them, so add that one library folder back (code only, no data)
_user_site = Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Roaming" / "Python" / \
    f"Python{sys.version_info[0]}{sys.version_info[1]}" / "site-packages"
if _user_site.is_dir() and str(_user_site) not in sys.path:
    sys.path.append(str(_user_site))

import uvicorn  # noqa: E402

from camctl import measure, setups, webui  # noqa: E402
import tempfile  # noqa: E402
from zve10_sim import ZVE10  # noqa: E402


def _fake_claude() -> None:
    """Simulator only: canned answers in place of Claude (no network, no key), clearly marked as made up."""
    import time as _t
    from camctl import assistant, bestpick, shotplan
    note = "(Simulator: a made-up answer, no Claude call.) "
    assistant.has_key = lambda: True

    def _steps(settings, wants):
        by = {s["name"]: s for s in settings}
        out = []
        for name, k in wants:
            s = by.get(name)
            if not s or not s.get("choices"):
                continue
            vals = [c for c in s["choices"] if c["value"] not in (0, 0xFFFFFF)]
            i = next((j for j, c in enumerate(vals) if c["value"] == s["value"]), None)
            if i is None:
                continue
            j = max(0, min(len(vals) - 1, i + k))
            if j != i:
                out.append({"name": name, "value": vals[j]["value"], "label": vals[j]["label"], "from_label": s["label"], "same": False})
        return out

    def ask(jpg, settings, request):
        _t.sleep(2.5)
        return {"changes": _steps(settings, [("aperture", -2)]),
                "explanation": note + "I open the aperture so the background goes soft and your face stands out. "
                               "Keep the light low on the shadow side for the mood.", "tips": "", "dropped": []}

    def review(frame, settings, meas, shot):
        _t.sleep(2.5)
        return {"good": [note + "The framing is calm and the eyes sit on the upper third."],
                "fix": ["The shadow side is a bit heavy: open the aperture one step."],
                "changes": _steps(settings, [("aperture", -1)]), "dropped": []}

    def best(cands):
        _t.sleep(2.0)
        return {"best": [cands[0][0]], "why": note + "The calmest expression and the clearest eye line."}

    def plan(refs, live, settings, request, mode, gear=""):
        _t.sleep(2.5)
        return {"title": "Soft window key", "key_fill_ratio": "3:1", "contrast": "medium",
                "look_analysis": note + "One soft light from the left at 45 degrees, a white board on the right for fill.",
                "lights": [{"role": "key", "fixture": "LED panel", "modifier": "60 cm softbox", "angle_deg": -45, "distance_m": 1.2,
                            "height_m": 1.8, "power_stops": 0, "color_temp_k": 5600, "gel": "none", "note": ""},
                           {"role": "fill", "fixture": "white foam board", "modifier": "bounce", "angle_deg": 50, "distance_m": 1.0,
                            "height_m": 1.4, "power_stops": -1.6, "color_temp_k": 0, "gel": "none", "note": ""}],
                "background": "keep it a stop darker than your face", "practicals": "none",
                "steps": ["Put the softbox 45 degrees to your left, a bit above eye height.", "Hold the board on the right until the shadow side opens up."],
                "camera": [], "camera_notes": "", "dropped": []}

    assistant.ask = ask
    assistant.lighting_plan = plan
    shotplan.claude_review = review
    bestpick.ask_claude = best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ev", type=float, default=8.0)
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--battery", type=int, default=80)
    ap.add_argument("--nocam", action="store_true", help="no camera connected (to see the offline screen)")
    ap.add_argument("--model", choices=["SIM-A", "SIM-FX", "SIM-B"], help="an INVENTED other camera model (test data, not a real camera)")
    ap.add_argument("--no-key", action="store_true", help="with --fake-claude: start without a Claude key (to test adding one)")
    ap.add_argument("--fake-claude", action="store_true", help="canned Claude answers (simulator only, no API call) to test the Claude panel")
    ap.add_argument("--night", action="store_true", help="exposed for a dark room: f/4 1/125 ISO 6400 (noisy stills)")
    ap.add_argument("--split", type=float, default=0.0, help="stops darker on the shadow half of the face (key light from the side)")
    a = ap.parse_args()
    cam = ZVE10(ev=a.ev, mode="video" if a.video else "photo", battery=a.battery, model=a.model)
    cam.split = a.split
    if a.night:
        from camctl.drivers.sony import PROPS
        cam.p[PROPS["aperture"]].value = 400
        cam.p[PROPS["iso"]].value = 6400
    measure._faces_cv = lambda im: cam.face_box_px(im.width, im.height)
    webui.assistant.has_key = lambda: False             # the simulator never spends Claude tokens
    webui.assistant._key = lambda: ""                     # ...not even through a path that reads the key itself
    tmp = Path(tempfile.mkdtemp(prefix="camctl_sim_"))      # never touch his real prefs, takes or captures
    from camctl import keystore
    keystore.BASE, keystore.PATH = tmp, tmp / "claude_key.bin"   # a key typed in the simulator stays in its temp folder
    webui.assistant.key_source = lambda: "app" if keystore.saved() else ""

    def _fake_test(key):                                    # never a real request: canned answers by the fake key's name
        import time as _t
        _t.sleep(1.2)
        if "wrong" in key:
            return "wrong", "Claude didn't accept this key. Check that you copied all of it, or make a new one."
        if "nocredit" in key:
            return "nocredit", "The key works, but the account has no credit. Add credit at console.anthropic.com."
        return "ok", "Claude is connected. (Simulator: no real request was made.)"
    webui.assistant.test_key = _fake_test
    if a.fake_claude:
        _fake_claude()
        if a.no_key:                                       # the panel starts without a key; saving one turns it on
            webui.assistant.has_key = lambda: keystore.saved()
    setups.BASE = tmp
    from camctl import camprofile
    camprofile.BASE, camprofile.SAVED = tmp, tmp / "camera_profiles.json"    # check results stay in the temp folder
    setups.SETUP_DIR = tmp / "setups"                       # never his real setups / gear
    setups.GEAR_FILE = tmp / "gear.json"
    webui.CAPTURE_DIR = tmp / "captures"
    webui.handoff.SHOOTS = tmp / "shoots"                    # simulated shoot folders, not his Pictures
    webui.handoff.STATE = tmp / "shoot.json"
    st = webui.Studio("usb")
    st.cam = None if a.nocam else cam

    def _no_cam():
        raise webui.PTPError("Camera not found on USB: switch it on and check the cable (USB Connection: PC Remote).")
    st._new_cam = _no_cam if a.nocam else (lambda: cam)      # never look for real hardware
    if a.nocam:
        st._present = lambda: False
    app = webui.build_app(st, start_auto=False)
    import gc
    from camctl import autofix
    for o in gc.get_objects():                     # the Operator, so "Analyse again" and setups finish
        if isinstance(o, autofix.AutoFix) and o.studio is st:
            o.start()
    print(f"SIMULATED camera Studio on http://127.0.0.1:{a.port}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
