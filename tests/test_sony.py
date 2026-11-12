from camctl.drivers.sony import SonyCamera, format_value
from fake_sony import FakeSony, JPEG, RAW

def test_full_flow():
    fake = FakeSony()
    cam = SonyCamera("127.0.0.1", fake.port)
    cam.connect()
    assert cam.name == "ZV-E10"
    assert [o[0] for o in fake.ops[:5]] == [0x1002, 0x9201, 0x9201, 0x9202, 0x9201]
    s = {e["name"]: e for e in cam.settings()}
    assert s["iso"]["value"] == 100 and 0xFFFFFF in s["iso"]["choices"]
    cam.set("aperture", 400)
    assert format_value(0x5007, cam.refresh()[0x5007].value) == "f/4"
    cam.set("iso", 0xFFFFFF)
    assert format_value(0xD21E, cam.refresh()[0xD21E].value) == "auto"
    assert cam.live_view() == JPEG
    assert cam.capture(timeout=5) == [(0xB101, RAW), (0x3801, JPEG)]
    cam.close()


def test_take_photo_tool_saves_files(tmp_path, monkeypatch):
    from camctl import server
    fake = FakeSony()
    cam = SonyCamera("127.0.0.1", fake.port)
    cam.connect()
    monkeypatch.setattr(server, "_cam", cam)
    monkeypatch.setattr(server, "CAPTURE_DIR", tmp_path)
    monkeypatch.setattr(server, "_preview", lambda jpeg: jpeg)
    result = server.camera_take_photo()
    text, image = result
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".arw", ".jpg"]
    assert str(tmp_path) in text
    assert image.data == JPEG
    cam.close()


def test_shutter_holds_half_press_before_release(monkeypatch):
    """Real ZV-E10: S2 is ignored unless S1 is held long enough first (verified 11 Nov 2026)."""
    from camctl.drivers import sony as S
    cam = S.SonyCamera.__new__(S.SonyCamera)
    seq = []
    monkeypatch.setattr(cam, "_control", lambda code, val: seq.append((hex(code), val)), raising=False)
    monkeypatch.setattr(cam, "refresh", lambda: {}, raising=False)
    monkeypatch.setattr(cam, "fetch_pending", lambda timeout=20, **kw: [], raising=False)
    sleeps = []
    monkeypatch.setattr(S.time, "sleep", lambda s: sleeps.append((len(seq), s)))
    cam.capture(af=False)
    assert seq == [("0xd2c1", 2), ("0xd2c2", 2), ("0xd2c2", 1), ("0xd2c1", 1)]
    held = sum(s for n, s in sleeps if n == 1)              # time between S1 down and S2 down
    assert held >= S.S1_SETTLE_S >= 1.0


def test_transfer_stops_as_soon_as_raw_and_jpeg_are_in():
    """12 Nov 2026: after both files had arrived Studio still waited 1.5 s for 'nothing more'."""
    import struct
    import time as _t
    from camctl.drivers import sony

    class _R:
        def __init__(self, data, ok=True):
            self.data, self.ok, self.code = data, ok, 0x2001
    queue = [0xB101, sony.FMT_JPEG]

    class _C:
        def transaction(self, op, params, data_out=None):
            if op == sony.OC_GET_OBJECT_INFO:
                return _R(b"\x00" * 4 + struct.pack("<H", queue[0]) + b"\x00" * 10)
            fmt = queue.pop(0)
            return _R(b"\xff\xd8jpeg" if fmt == sony.FMT_JPEG else b"arw-data")
    cam = sony.SonyCamera.__new__(sony.SonyCamera)
    cam.client = _C()
    cam._pending_objects = lambda: bool(queue)
    t0 = _t.time()
    files = cam.fetch_pending(timeout=5, expect={0xB101, sony.FMT_JPEG})
    assert {f for f, _ in files} == {0xB101, sony.FMT_JPEG} and _t.time() - t0 < 0.5
