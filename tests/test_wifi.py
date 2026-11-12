"""Wi-Fi (PTP/IP, PC Remote over an access point): pairing, stable GUID, discovery, Studio switching."""
import socket
import struct
import threading
import time

from camctl import ptpip as P
from camctl import webui, wifi
from camctl.drivers.sony import SonyCamera

from fake_sony import FakeSony


class PairingSony(FakeSony):
    """FakeSony that only accepts paired GUIDs; in pairing mode the 'user' confirms after a delay."""

    def __init__(self, pairing=True, confirm_after=0.2):
        self.paired, self.pairing, self.confirm_after, self.inits = set(), pairing, confirm_after, []
        super().__init__()

    def run(self):
        while True:
            cmd, _ = self.srv.accept()
            try:
                t, body = P.read_packet(cmd)
            except (P.PTPError, OSError):        # a reachability probe: connect + close
                continue
            guid = body[:16]
            self.inits.append((guid, body[16:].decode("utf-16-le", "ignore").split("\x00")[0]))
            if guid not in self.paired:
                if not self.pairing:
                    cmd.sendall(P.build_packet(P.INIT_FAIL, struct.pack("<I", 1)))
                    cmd.close()
                    continue
                time.sleep(self.confirm_after)          # the camera shows "pair with ...?" until OK
                self.paired.add(guid)
            self.send(cmd, P.INIT_COMMAND_ACK, struct.pack("<I", 7) + b"\0" * 16 + "ZV-E10".encode("utf-16-le") + b"\0\0")
            evt, _ = self.srv.accept()
            P.read_packet(evt)
            self.send(evt, P.INIT_EVENT_ACK)
            self._serve(cmd)

    def _serve(self, cmd):
        threading.Thread(target=_loop, args=(self, cmd), daemon=True).start()


def _loop(self, cmd):
    """A small operation loop: property list, live view, OK for everything else."""
    try:
        while True:
            t, body = P.read_packet(cmd)
            phase, op, tid = struct.unpack("<IHI", body[:10])
            params = list(struct.unpack(f"<{(len(body)-10)//4}I", body[10:]))
            reply = None
            if phase == P.DATA_OUT:
                P.read_packet(cmd); P.read_packet(cmd)
            if op == 0x9209:
                from fake_sony import prop_blob
                reply = prop_blob([(c, f, dt, v, ch) for c, (f, dt, v, ch) in self.props.items()])
            elif op == 0x1009 and params and params[0] == 0xFFFFC002:
                from fake_sony import JPEG
                reply = struct.pack("<II", 16, len(JPEG)) + b"\0" * 8 + JPEG
            if reply is not None:
                self.send(cmd, P.START_DATA, struct.pack("<IQ", tid, len(reply)))
                self.send(cmd, P.END_DATA, struct.pack("<I", tid) + reply)
            self.send(cmd, P.OPERATION_RESPONSE, struct.pack("<HI", 0x2001, tid))
    except (P.PTPError, OSError):
        pass


def _cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi, "BASE", tmp_path)
    monkeypatch.setattr(wifi, "CONFIG", tmp_path / "wifi.json")


def test_guid_is_stable_and_pairing_sticks(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    g1, g2 = wifi.guid(), wifi.guid()
    assert g1 == g2 and len(g1) == 16
    cam = PairingSony(pairing=True)
    c = SonyCamera("127.0.0.1", cam.port, guid=g1, name=wifi.client_name(), timeout=5)
    c.connect()                                      # waits for the 'user' to confirm the pairing prompt
    assert c.name == "ZV-E10" and cam.inits[0][1].startswith("camctl Studio")
    assert c.live_view() == b"\xff\xd8fakejpeg\xff\xd9"
    c.close()
    cam.pairing = False                              # pairing screen closed: only the paired GUID is accepted
    c = SonyCamera("127.0.0.1", cam.port, guid=g1, timeout=5)
    c.connect()
    c.close()
    other = SonyCamera("127.0.0.1", cam.port, guid=b"\x01" * 16, timeout=5)
    try:
        other.connect()
        raise AssertionError("an unpaired PC must be refused")
    except P.PTPError as e:
        assert "Pairing" in str(e)


def test_scan_finds_the_camera_port():
    cam = PairingSony()
    assert wifi.scan(["127.0.0"], port=cam.port, timeout=0.2) == ["127.0.0.1"]
    assert wifi.split("10.0.0.5:1234") == ("10.0.0.5", 1234) and wifi.split("10.0.0.5")[1] == 15740


def test_studio_wifi_mode_live_view_and_watchdog(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    cam = PairingSony()
    st = webui.Studio("usb")
    st.released = True
    st.set_connection("wifi", f"127.0.0.1:{cam.port}")
    assert wifi.load()["mode"] == "wifi"
    frame, seq = st.latest_frame(-1, timeout=5)
    assert frame == b"\xff\xd8fakejpeg\xff\xd9" and st.health()["state"] == "ok"
    # camera disappears: Studio reports it as not reachable on Wi-Fi
    st.shutdown()
    st.set_connection("wifi", "127.0.0.1:1")         # nothing listens there
    monkeypatch.setattr(wifi, "scan", lambda **k: [])
    for _ in range(2):                                # 1st: connect refused; 2nd (after backoff): probe
        st._retry_at = 0
        try:
            st.run(lambda c: None)
            raise AssertionError("should fail")
        except P.PTPError:
            pass
    assert "not reachable" in st.last_error
    st.fail_since = time.time() - 10
    h = st.health()
    assert h["state"] == "absent" and "Wi-Fi" in h["message"]
    st.set_connection("usb")
    assert st.host == "usb" and wifi.load()["mode"] == "usb"


def test_config_read_hiccup_never_switches_to_usb(tmp_path, monkeypatch):
    """A start-up read failure (file mid-write / locked) must not make Studio forget the Wi-Fi camera."""
    from camctl import wifi as w
    monkeypatch.setattr(w, "BASE", tmp_path)
    monkeypatch.setattr(w, "CONFIG", tmp_path / "wifi.json")
    w.save(mode="wifi", host="10.0.0.38", guid="ab" * 16)
    assert (tmp_path / "wifi.json.bak").exists() and not (tmp_path / "wifi.tmp").exists()
    (tmp_path / "wifi.json").write_text("{\"mode\": \"wi", encoding="utf-8")       # half-written file
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    cfg = w.load()
    assert cfg.get("mode") == "wifi" and cfg.get("host") == "10.0.0.38" and cfg.get("guid") == "ab" * 16
