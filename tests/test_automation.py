import time

from camctl import automation, webui
from camctl.drivers.sony import PROPS, SonyCamera
from fake_sony import FakeSony

SH_60, SH_30, SH_125 = (1 << 16) | 60, (1 << 16) | 30, (1 << 16) | 125


def make():
    fake = FakeSony()
    fake.props[PROPS["shutter_speed"]] = ["I", 6, SH_60, [SH_30, SH_60, SH_125]]
    fake.props[PROPS["focus_mode"]] = ["H", 4, 2, [1, 2]]
    cam = SonyCamera("127.0.0.1", fake.port)
    cam.connect()
    studio = webui.Studio("unused")
    studio.cam = cam
    saved = []
    auto = automation.Automation(studio, lambda files: saved.extend(f"f{len(saved) + i}" for i, _ in enumerate(files)) or
                                 [f"f{len(saved)}"], lambda c, k, l: {"preset": k, "look": l})
    return fake, auto


def wait_done(auto, timeout=60):
    t = time.time()
    while auto.status["running"] and time.time() - t < timeout:
        time.sleep(0.1)
    assert not auto.status["running"], auto.status


def test_timelapse_and_bracket_restore_shutter():
    fake, auto = make()
    auto.start("timelapse", {"interval": 2, "count": 2, "delay": 0})
    wait_done(auto)
    assert auto.status["step"] == 2 and "Finished." in auto.status["message"]
    auto.start("bracket", {"frames": 3, "ev": 1})
    wait_done(auto)
    sets = [d for op, p, d in fake.ops if op == 0x9205 and p and p[0] == PROPS["shutter_speed"]]
    assert len(sets) == 4                                  # 3 frames + restore
    assert fake.props[PROPS["shutter_speed"]][2] == SH_60  # restored


def test_sequence_and_stop():
    fake, auto = make()
    auto.start("sequence", {"steps": [{"type": "shoot"}, {"type": "wait", "seconds": 30}, {"type": "shoot"}]})
    time.sleep(6)
    auto.stop()
    wait_done(auto)
    assert auto.status["step"] == 2 and "Stopped" in auto.status["message"]
    try:
        auto.start("nope", {})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown automation should fail")
