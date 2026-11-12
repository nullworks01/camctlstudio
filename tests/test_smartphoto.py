"""Smart photo: shoots only while armed, only on good steady frames, honours the limit, picks the best 3."""
import time

from camctl import handoff, smartphoto


class _St:
    def __init__(self):
        self.frame, self.frame_seq, self.last_view_req = b"x", 0, 0.0


def _good(ratio=1.2, stops=0.1, box=(0.4, 0.2, 0.2, 0.3)):
    return {"checks": [{"key": "exposure", "status": "ok", "label": "ok", "stops": stops},
                       {"key": "focus", "status": "ok", "label": "sharp", "ratio": ratio}],
            "overlays": {"subject_is_face": True, "subject": list(box)}}


def test_waits_for_good_steady_frame_limit_and_picks(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "Shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    sh = handoff.new_shoot("Smart")
    monkeypatch.setattr(smartphoto, "COOLDOWN_S", 0.0)
    st = _St()
    frames = iter([{"checks": [], "overlays": {"subject_is_face": False}}] * 2 + [_good(r) for r in
                  (1.0, 1.0, 1.0, 1.5, 1.5, 1.5, 0.95, 0.95, 0.95, 2.0, 2.0, 2.0, 1.1, 1.1, 1.1)] + [_good()] * 100)
    shots = []

    def shoot():
        n = len(shots) + 1
        f = tmp_path / f"p{n}.jpg"
        f.write_bytes(b"\xff\xd8" + bytes([n]))
        shots.append(f)
        return {"take": n, "shoot": [str(f)]}
    sp = smartphoto.SmartPhoto(st, lambda fr: next(frames), shoot)

    def feed():
        for _ in range(300):
            st.frame_seq += 1
            time.sleep(0.05)
    import threading
    threading.Thread(target=feed, daemon=True).start()
    sp.arm(limit=4)
    assert sp.status["armed"]
    for _ in range(200):
        if not sp.status["armed"]:
            break
        time.sleep(0.05)
    assert not sp.status["armed"] and sp.status["shots"] == 4 and len(shots) == 4
    assert len(sp.status["picks"]) == 3
    picks = sorted(p.name for p in (handoff.SHOOTS / sh["path"] / "PICKS").iterdir())
    assert len(picks) == 3 and "p3.jpg" not in picks                    # the softest one is left out
    assert all(f.exists() for f in shots)                                # originals stay


def test_not_ready_without_face_or_when_moving():
    sp = smartphoto.SmartPhoto(_St(), lambda f: {}, lambda: {})
    assert sp.ready({"checks": [], "overlays": {"subject_is_face": False}})[0] is False
    for b in ((0.1, 0.1, 0.2, 0.2), (0.3, 0.1, 0.2, 0.2), (0.5, 0.1, 0.2, 0.2)):
        ok, why = sp.ready(_good(box=b))
    assert not ok and "still" in why
    sp.disarm()
    assert smartphoto.HARD_LIMIT == 30
