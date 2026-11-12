"""Focus stack planning from a sweep (no photos) and the focus returning to where it was after the series."""
import numpy as np

from camctl import stackplan


def _samples(peaks, positions=range(0, 60, 4)):
    """Tiles whose sharpness peaks at the given focus positions (rows x cols filled from the list)."""
    out = []
    peaks = np.array(peaks, dtype=float).reshape(6, 8)
    for p in positions:
        out.append((p, 100.0 * np.exp(-((p - peaks) / 6.0) ** 2)))
    return out


def test_good_when_near_and_far_differ():
    peaks = np.linspace(8, 40, 48)
    r = stackplan.plan(_samples(peaks))
    assert r["ok"] and r["kind"] == "good" and r["start"] <= 12 and r["end"] >= 36


def test_everything_already_sharp():
    r = stackplan.plan(_samples([22] * 48))
    assert not r["ok"] and r["kind"] == "all_sharp" and "already sharp" in r["verdict"]


def test_too_close():
    r = stackplan.plan(_samples([0] * 48))
    assert not r["ok"] and r["kind"] == "too_close" and r["verdict"].startswith("Too close")


def test_a_face_is_warned():
    r = stackplan.plan(_samples(np.linspace(8, 40, 48)), has_face=True)
    assert "still objects" in r["verdict"]


def test_stack_returns_the_focus_to_where_it_was():
    from camctl import automation

    class _Cam:
        pos = 37
        shots = 0

        def refresh(self):
            class P:
                pass
            p = P(); p.value = max(0, min(100, self.pos))
            return {0xD24C: p}

        def focus_step(self, s):
            self.pos = max(0, min(100, self.pos + int(round(s * 1.3))))   # uneven steps like the real lens

        def set(self, *a):
            return None

    cam = _Cam()
    a = automation.Automation.__new__(automation.Automation)
    a.status = {"running": True, "step": 0, "total": 0, "message": ""}
    a._cam = lambda fn: fn(cam)
    a._lock = lambda: {}
    a._restore = lambda snap: None
    a._log = lambda m: None
    a._wait = lambda s: None
    a._check = lambda: None
    a._shoot = lambda label, required=False: setattr(cam, "shots", cam.shots + 1)
    a._focus_stack({"shots": 5, "start": 10, "end": 30})
    assert cam.shots == 5 and abs(cam.pos - 37) <= 1                         # back where it was
    assert a.status["positions"][0] <= 12 and a.status["positions"][-1] >= 28
