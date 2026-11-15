"""SIMULATED Wi-Fi cameras of other makers, for Studio's (untested on real cameras) Wi-Fi drivers.

They speak what the drivers expect from the public protocol notes (see docs/cameras.md, "Where the codes come
from"); they are not recordings of real cameras. Everything listens on 127.0.0.1 only.

  FakeLumix    Panasonic cam.cgi over HTTP, live picture as UDP packets
  FakeOlympus  OM System / Olympus .cgi over HTTP, live picture as RTP over UDP
  FakeRicoh    Ricoh / Pentax JSON /v1 API over HTTP, live picture as an M-JPEG stream
  FakeFuji     Fujifilm PC Shoot: the 82-byte init on one TCP port, then PTP containers; live JPEGs on port + 2
"""
from __future__ import annotations

import io
import json
import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LABEL = ""            # the simulator sets it: frames then show a simple scene with this label
PHOTO = ""            # or a photo file (the simulator's --photo): every frame is that photo, 800 x 450
_scene: dict = {}


def tiny_jpeg(shade: int = 128) -> bytes:
    if PHOTO:
        if PHOTO not in _scene:
            from PIL import Image
            im = Image.open(PHOTO).convert("RGB")
            w, h = im.size
            th = int(w * 9 / 16)
            if th < h:
                im = im.crop((0, (h - th) // 2, w, (h - th) // 2 + th))
            b = io.BytesIO()
            im.resize((800, 450)).save(b, "JPEG", quality=78)
            _scene[PHOTO] = b.getvalue()
        return _scene[PHOTO]
    if LABEL:
        if LABEL not in _scene:
            from PIL import Image, ImageDraw
            from fake_ptp_brands import jpeg
            im = Image.open(io.BytesIO(jpeg(640, 424)))
            ImageDraw.Draw(im).text((400, 210), LABEL, fill=(255, 255, 255))     # right of the face, clear of Studio's lines
            b = io.BytesIO()
            im.save(b, "JPEG", quality=80)
            _scene[LABEL] = b.getvalue()
        return _scene[LABEL]
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (shade, shade // 2, 200 - shade // 2)).save(buf, "JPEG", quality=70)
    return buf.getvalue()


class _HTTPFake:
    """A threaded HTTP server on 127.0.0.1 that hands each request to self.handle(method, path, query, body)."""

    def __init__(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _go(self, method):
                u = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append((method, u.path, u.query))
                fake.headers_seen.append(dict(self.headers))
                res = fake.handle(method, u.path.lstrip("/"), dict(urllib.parse.parse_qsl(u.query)), body, self)
                if res is None:                       # the handler streamed its own answer
                    return
                code, ctype, data = res
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

        self.requests: list[tuple[str, str, str]] = []
        self.headers_seen: list[dict] = []
        self.shots = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._udp_stop = threading.Event()

    def close(self):
        self._udp_stop.set()
        self.server.shutdown()
        self.server.server_close()

    def handle(self, method, path, q, body, h):
        raise NotImplementedError

    def _udp_sender(self, port: int, packets_for_frame):
        """Sends one frame every 0.1 s to 127.0.0.1:port until close() or a new stream."""
        self._udp_stop.set()
        stop = threading.Event()
        self._udp_stop = stop

        def run():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            i = 0
            while not stop.is_set():
                for pkt in packets_for_frame(i):
                    s.sendto(pkt, ("127.0.0.1", port))
                i += 1
                time.sleep(0.1)
            s.close()
        threading.Thread(target=run, daemon=True).start()


# -- Panasonic LUMIX -------------------------------------------------------------------------------------------------
class FakeLumix(_HTTPFake):
    def __init__(self, ask_first: int = 0):
        super().__init__()
        self.ask_first = ask_first             # answers "ok_under_research" this many times (the camera's prompt)
        self.s = {"shtrspeed": "2560/256", "focal": "1280/256", "iso": "400"}
        self.rec = False
        self.mode = "play"

    def handle(self, method, path, q, body, h):
        def ok(extra=""):
            return 200, "text/xml", f'<?xml version="1.0" encoding="UTF-8"?><camrply><result>ok</result>{extra}</camrply>'.encode()
        if path != "cam.cgi":
            return 404, "text/plain", b"no"
        m = q.get("mode")
        if m == "accctrl":
            if self.ask_first > 0:
                self.ask_first -= 1
                return 200, "text/xml", b"<camrply><result>ok_under_research</result></camrply>"
            return ok()
        if m == "camcmd":
            v = q.get("value")
            if v in ("recmode", "playmode"):
                self.mode = v[:-4]
            elif v == "capture":
                self.shots += 1
            elif v in ("video_recstart", "video_recstop"):
                self.rec = v == "video_recstart"
            return ok()
        if m == "getstate":
            return ok(f"<state><batt>2/3</batt><rec>{'on' if self.rec else 'off'}</rec></state><model>DC-GH5</model>")
        if m == "getsetting":
            t = q.get("type")
            if t not in self.s:
                return 200, "text/xml", b"<camrply><result>err_param</result></camrply>"
            return ok(f'<settingvalue {t}="{self.s[t]}"></settingvalue>')
        if m == "setsetting":
            self.s[q["type"]] = q["value"]
            return ok()
        if m == "startstream":
            port = int(q["value"])
            self._udp_sender(port, lambda i: [b"\x00" * 32 + tiny_jpeg(60 + i % 100)])
            return ok()
        if m == "stopstream":
            self._udp_stop.set()
            return ok()
        if m == "camctrl":
            return ok()
        return 200, "text/xml", b"<camrply><result>err_non_support</result></camrply>"


# -- OM System / Olympus ---------------------------------------------------------------------------------------------
class FakeOlympus(_HTTPFake):
    def __init__(self):
        super().__init__()
        self.p = {"shutspeedvalue": ("250", "60 125 250 500 1000"), "focalvalue": ("5.6", "2.8 4.0 5.6 8.0 11"),
                  "isospeedvalue": ("Auto", "Auto 200 400 800 1600"), "expcomp": ("0.0", "-1.0 -0.7 -0.3 0.0 0.3 0.7 1.0"),
                  "takemode": ("A", "iAuto P A S M")}
        self.mode = "play"
        self.files: list[str] = []

    def handle(self, method, path, q, body, h):
        if path == "get_caminfo.cgi":
            return 200, "text/xml", b"<?xml version='1.0'?><caminfo><model>E-M10MarkII</model></caminfo>"
        if path == "switch_cammode.cgi":
            self.mode = q.get("mode", "")
            return 200, "text/plain", b""
        if self.mode != "rec" and path in ("get_camprop.cgi", "set_camprop.cgi", "exec_takemotion.cgi"):
            return 520, "text/plain", b"wrong mode"
        if path == "get_camprop.cgi":
            parts = "".join(f"<desc><propname>{k}</propname><attribute>getset</attribute><value>{v}</value>"
                            f"<enum>{e}</enum></desc>" for k, (v, e) in self.p.items())
            return 200, "text/xml", f"<?xml version='1.0'?><desclist>{parts}</desclist>".encode()
        if path == "set_camprop.cgi":
            v = body.decode().split("<value>")[1].split("</value>")[0]
            k = q["propname"]
            self.p[k] = (v, self.p[k][1])
            return 200, "text/plain", b""
        if path == "exec_takemotion.cgi":
            if q.get("com") == "starttake":
                self.shots += 1
                self.files.append(f"P{self.shots:07d}")
                return 200, "text/xml", b"<?xml version='1.0'?><take>ok</take>"
            if q.get("com") == "assignafframe":
                return 200, "text/xml", b"<?xml version='1.0'?><affocus>ok</affocus>"
        if path == "get_imglist.cgi":
            d = q.get("DIR")
            if d == "/DCIM":
                return 200, "text/plain", b"VER_100\r\n/DCIM,100OLYMP,0,16,0,0\r\n"
            rows = "".join(f"/DCIM/100OLYMP,{n}.{x},1000,0,20000,{i}\r\n" for i, n in enumerate(self.files)
                           for x in ("JPG", "ORF"))
            return 200, "text/plain", ("VER_100\r\n" + rows).encode()
        if path.startswith("DCIM/100OLYMP/"):
            name = path.rsplit("/", 1)[1]
            return 200, "application/octet-stream", tiny_jpeg() if name.endswith(".JPG") else b"ORF-RAW" + name.encode()
        if path == "exec_takemisc.cgi":
            if q.get("com") == "startliveview":
                port = int(q["port"])

                def rtp(i):
                    jpg = tiny_jpeg(60 + i % 100)
                    half = len(jpg) // 2
                    return [bytes([0x80, 0x60]) + struct.pack(">HI", i * 2, i) + b"\0\0\0\1" + jpg[:half],
                            bytes([0x80, 0xE0]) + struct.pack(">HI", i * 2 + 1, i) + b"\0\0\0\1" + jpg[half:]]
                self._udp_sender(port, rtp)
            else:
                self._udp_stop.set()
            return 200, "text/plain", b""
        return 404, "text/plain", b"no"


# -- Ricoh / Pentax --------------------------------------------------------------------------------------------------
class FakeRicoh(_HTTPFake):
    def __init__(self):
        super().__init__()
        self.props = {"model": "RICOH GR III", "battery": 77, "av": "5.6", "avList": ["2.8", "4.0", "5.6", "8.0"],
                      "tv": "1.250", "tvList": ["1.60", "1.125", "1.250", "1.500", "2.1"],
                      "sv": "auto", "svList": ["auto", "100", "200", "400", "800"],
                      "xv": "0.0", "xvList": ["-0.7", "-0.3", "0.0", "0.3", "0.7"],
                      "WBMode": "auto", "WBModeList": ["auto", "daylight", "shade", "tungsten"],
                      "captureMode": "still", "stateMovie": "idle"}
        self.photos: list[str] = []

    def handle(self, method, path, q, body, h):
        def js(d):
            return 200, "application/json", json.dumps(d).encode()
        if path == "v1/props":
            return js({"errCode": 200, **self.props})
        if path == "v1/params/camera" and method == "PUT":
            for k, v in urllib.parse.parse_qsl(body.decode()):
                self.props[k] = v
            return js({"errCode": 200})
        if path == "v1/camera/shoot":
            self.shots += 1
            self.photos.append(f"R{self.shots:07d}")
            return js({"errCode": 200, "captured": True})
        if path in ("v1/camera/shoot/start", "v1/camera/shoot/finish"):
            self.props["stateMovie"] = "recording" if path.endswith("start") else "idle"
            return js({"errCode": 200})
        if path == "v1/lens/focus":
            return js({"errCode": 200, "focused": True})
        if path == "v1/photos":
            return js({"errCode": 200, "dirs": [{"name": "100RICOH", "files": [f"{n}.{x}" for n in self.photos
                                                                           for x in ("JPG", "DNG")]}]})
        if path.startswith("v1/photos/100RICOH/"):
            name = path.rsplit("/", 1)[1]
            return 200, "application/octet-stream", tiny_jpeg() if name.endswith(".JPG") else b"DNG-RAW" + name.encode()
        if path == "v1/liveview":
            h.send_response(200)
            h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            h.end_headers()
            try:
                for i in range(600):
                    jpg = tiny_jpeg(60 + i % 100)
                    h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
                    h.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return None
        return 404, "application/json", b'{"errCode":404,"errMsg":"Not Found"}'


# -- Fujifilm --------------------------------------------------------------------------------------------------------
def _free_triple() -> int:
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
        s.close()
        if base > 65000:
            continue
        try:
            for p in (base + 1, base + 2):
                t = socket.socket()
                t.bind(("127.0.0.1", p))
                t.close()
            return base
        except OSError:
            continue
    raise RuntimeError("no three free ports in a row")


class FakeFuji:
    """Fujifilm PC Shoot: init on port, PTP containers after; event port + 1, live JPEGs on port + 2 once
    InitiateOpenCapture came. refuse=True answers the init with a fail packet (not paired)."""

    def __init__(self, refuse: bool = False, empty_device_info: bool = True):
        self.refuse = refuse
        self.empty_device_info = empty_device_info
        self.port = _free_triple()
        self.host = f"127.0.0.1:{self.port}"
        self.props = {0xDF01: 0, 0xDF24: 0x00020001}            # the Fuji start-sequence values
        self.recording = False
        self.ops: list[int] = []
        self.shots = 0
        self.capture_open = threading.Event()
        self._stop = threading.Event()
        self.socks = []
        for i, fn in enumerate((self._serve_cmd, self._serve_evt, self._serve_jpg)):
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.port + i))
            s.listen(2)
            self.socks.append(s)
            threading.Thread(target=self._accept, args=(s, fn), daemon=True).start()

    def close(self):
        self._stop.set()
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass

    def _accept(self, srv, fn):
        while not self._stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=fn, args=(c,), daemon=True).start()

    @staticmethod
    def _recv(c, n):
        b = b""
        while len(b) < n:
            x = c.recv(n - len(b))
            if not x:
                raise OSError("closed")
            b += x
        return b

    def _block(self, c):
        n = struct.unpack("<I", self._recv(c, 4))[0]
        return self._recv(c, n - 4)

    def _send(self, c, ptype, code, tid, payload=b""):
        body = struct.pack("<HHI", ptype, code, tid) + payload
        c.sendall(struct.pack("<I", 4 + len(body)) + body)

    def _serve_cmd(self, c):
        from fake_ptp_brands import FakePlainPTP
        brain = self.brain = getattr(self, "brain", None) or FakePlainPTP()     # the standard PTP part
        brain.OPS = brain.OPS + [0x101C, 0x9020, 0x9021]
        try:
            init = self._block(c)
            assert struct.unpack_from("<II", init, 0) == (1, 0x8F53E4F2), "bad init"
            if self.refuse:
                c.sendall(struct.pack("<III", 16, 5, 0x2019) + b"\0" * 4)
                return
            name = "X-T4".encode("utf-16-le") + b"\0\0"
            body = struct.pack("<II", 2, 1) + b"\x11" * 16 + name
            c.sendall(struct.pack("<I", 4 + len(body)) + body)
            pending = None
            while not self._stop.is_set():
                b = self._block(c)
                ptype, code, tid = struct.unpack_from("<HHI", b, 0)
                n = (len(b) - 8) // 4
                params = list(struct.unpack(f"<{n}I", b[8:8 + n * 4]))
                if ptype == 2:                                     # data phase of SetDevicePropValue
                    prop, pending = pending, None
                    if prop in (0xDF01, 0xDF24):
                        self.props[prop] = int.from_bytes(b[8:12].ljust(4, b"\0"), "little")
                        self._send(c, 3, 0x2001, tid)
                    else:
                        r = brain.transaction(0x1016, [prop], b[8:])
                        self._send(c, 3, r.code, tid)
                    continue
                self.ops.append(code)
                if code == 0x1016:
                    pending = params[0]
                    continue
                if code == 0x1001 and self.empty_device_info:
                    self._send(c, 2, code, tid, b"")
                    self._send(c, 3, 0x2001, tid)
                elif code == 0x1015 and params and params[0] in (0xDF01, 0xDF24):
                    self._send(c, 2, code, tid, self.props[params[0]].to_bytes(4 if params[0] == 0xDF24 else 2, "little"))
                    self._send(c, 3, 0x2001, tid)
                elif code == 0x101C:
                    self.capture_open.set()
                    self._send(c, 3, 0x2001, tid)
                elif code in (0x9020, 0x9021):
                    self.recording = code == 0x9020
                    self._send(c, 3, 0x2001, tid)
                else:
                    if code == 0x100E:
                        self.shots += 1
                    r = brain.transaction(code, params)
                    if r.data:
                        self._send(c, 2, code, tid, r.data)
                    self._send(c, 3, r.code, tid, b"".join(struct.pack("<I", x) for x in r.params))
        except (OSError, AssertionError):
            pass
        finally:
            c.close()

    def _serve_evt(self, c):
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            c.close()

    def _serve_jpg(self, c):
        try:
            i = 0
            while not self._stop.is_set():
                data = b"\0" * 14 + tiny_jpeg(60 + i % 100)
                c.sendall(struct.pack("<I", 4 + len(data)) + data)
                i += 1
                time.sleep(0.1)
        except OSError:
            pass
        finally:
            c.close()
