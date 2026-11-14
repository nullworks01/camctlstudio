"""Fujifilm over Wi-Fi ("PC Shoot" / remote shooting over the network, the protocol FUJIFILM X Acquire uses).

UNTESTED ON REAL CAMERAS. Written from libgphoto2 (camlibs/ptp2/fujiptpip.c and library.c, LGPL; packet layout and
codes only, no code copied) and tested against a simulated camera only.

How it differs from PTP/IP: only the first packet is PTP/IP-like (a fixed 82-byte Init Command Request with the magic
number 0x8F53E4F2, this PC's GUID and name). After that the camera speaks the USB container format over TCP port
55740: <u32 length><u16 type 1 command / 2 data / 3 response / 4 event><u16 code><u32 transaction id><payload>.
After OpenSession Studio sends the start sequence the camera expects (property 0xDF01 = 5, the app version 0xDF24 read
and written back, InitiateOpenCapture); the camera then opens port 55741 (events) and 55742 (live view: one JPEG per
packet, after a short header). Over USB a Fujifilm stays on the plain PTP driver (ptpstd.py).
"""
from __future__ import annotations

import socket
import struct
import threading

from ..ptpip import PTPError, Response
from .httpcam import FrameStream, jpeg_between
from .ptpstd import OC_SET_DEVICE_PROP_VALUE, PTPCamera, Unsupported

FUJI_PORT = 55740
FUJI_MAGIC = 0x8F53E4F2
INIT_PACKET = 82
OC_GET_DEVICE_PROP_VALUE = 0x1015
OC_INITIATE_OPEN_CAPTURE = 0x101C
OC_FUJI_MOVIE_START = 0x9020
OC_FUJI_MOVIE_STOP = 0x9021
DP_FUJI_INIT_SEQUENCE = 0xDF01
DP_FUJI_APP_VERSION = 0xDF24
T_COMMAND, T_DATA, T_RESPONSE, T_EVENT = 1, 2, 3, 4


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PTPError("connection closed by camera")
        buf += chunk
    return bytes(buf)


def read_block(sock: socket.socket) -> bytes:
    """One <u32 length><rest> block; returns rest."""
    n = struct.unpack("<I", _recv_exact(sock, 4))[0]
    if n < 4 or n > 512 << 20:
        raise PTPError(f"bad packet length {n}")
    return _recv_exact(sock, n - 4)


def init_request(guid: bytes, name: str) -> bytes:
    pkt = bytearray(INIT_PACKET)
    struct.pack_into("<III", pkt, 0, INIT_PACKET, 1, FUJI_MAGIC)
    pkt[12:28] = (guid + bytes(16))[:16]
    enc = name[:(INIT_PACKET - 28 - 1) // 2].encode("utf-16-le", "replace")
    pkt[28:28 + len(enc)] = enc
    return bytes(pkt)


class FujiPTPIPClient:
    """Transport with the same transaction() API as PTPIPClient / PTPUSBClient."""

    def __init__(self, host: str, port: int = FUJI_PORT, name: str = "camctl", guid: bytes | None = None,
                 timeout: float = 10.0):
        self.host, self.port, self.name = host, port or FUJI_PORT, name
        self.guid = guid or bytes(16)
        self.timeout = timeout
        self.cmd = self.evt = self.jpg = None
        self.camera_name = ""
        self._tid = 0
        self._lock = threading.Lock()

    def connect(self) -> None:
        try:
            self.cmd = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise PTPError(f"The Fujifilm camera at {self.host} does not accept a PC connection right now "
                           "(set it to remote shooting / PC Shoot over Wi-Fi).") from exc
        self.cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.cmd.sendall(init_request(self.guid, self.name))
        body = read_block(self.cmd)                       # <u32 type><u32 conn><16 guid><name>
        ptype = struct.unpack_from("<I", body, 0)[0]
        if ptype == 5:
            self.close()
            raise PTPError("The Fujifilm camera refused the connection. Pair it with this PC once on the camera "
                           "(Connection Setting › PC Shoot / network setting), then try again.")
        if ptype != 2:
            raise PTPError(f"unexpected packet {ptype} during init")
        self.camera_name = body[24:].decode("utf-16-le", "ignore").split("\x00")[0]
        self.cmd.settimeout(min(self.timeout, 15.0))

    def open_streams(self) -> None:
        """Event and live-view sockets: the camera listens on them after InitiateOpenCapture."""
        for attr, port in (("evt", self.port + 1), ("jpg", self.port + 2)):
            try:
                s = socket.create_connection((self.host, port), 5.0)
                setattr(self, attr, s)
            except OSError:
                setattr(self, attr, None)
        if self.evt is not None:
            threading.Thread(target=self._drain_events, daemon=True, name="fuji-events").start()

    def _drain_events(self) -> None:
        try:
            self.evt.settimeout(None)
            while True:
                read_block(self.evt)
        except (OSError, PTPError, AttributeError):
            pass

    def close(self) -> None:
        for s in (self.cmd, self.evt, self.jpg):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.cmd = self.evt = self.jpg = None

    def transaction(self, opcode: int, params=(), data_out: bytes | None = None) -> Response:
        if self.cmd is None:
            raise PTPError("not connected")
        with self._lock:
            self._tid += 1
            tid = self._tid
            req = struct.pack("<HHI", T_COMMAND, opcode, tid) + b"".join(struct.pack("<I", p & 0xFFFFFFFF)
                                                                          for p in params)
            self.cmd.sendall(struct.pack("<I", 4 + len(req)) + req)
            if data_out is not None:
                d = struct.pack("<HHI", T_DATA, opcode, tid) + data_out
                self.cmd.sendall(struct.pack("<I", 4 + len(d)) + d)
            data = bytearray()
            while True:
                body = read_block(self.cmd)
                ptype, code, _rtid = struct.unpack_from("<HHI", body, 0)
                if ptype == T_DATA:
                    data += body[8:]
                elif ptype == T_RESPONSE:
                    rest = body[8:]
                    return Response(code, list(struct.unpack(f"<{len(rest) // 4}I", rest[:len(rest) // 4 * 4])),
                                    bytes(data))

    def read_jpeg(self) -> bytes | None:
        if self.jpg is None:
            return None
        return jpeg_between(read_block(self.jpg))


class FujiCamera(PTPCamera):
    """Fujifilm X / GFX over Wi-Fi: standard PTP settings and shutter, plus the live picture from port 55742."""

    brand = "Fujifilm"
    vendor_id = 0x04CB
    kind = "fuji-wifi"

    def __init__(self, host: str = "usb", client=None, vendor: int | None = None, port: int | None = None,
                 name: str = "camctl", guid: bytes | None = None):
        if client is None and host != "usb":
            ip, _, p = host.partition(":")
            client = FujiPTPIPClient(ip, port or (int(p) if p.isdigit() else FUJI_PORT), name=name, guid=guid,
                                     timeout=60.0)
        super().__init__(host, client, vendor)
        self.stream: FrameStream | None = None
        self.recording = False

    def device_info(self) -> dict:
        r = self.client.transaction(0x1001)
        if r.ok and r.data:
            try:
                return super().device_info()
            except Exception:  # noqa: BLE001 - some bodies send an empty dataset over Wi-Fi
                pass
        # the X-T4 answers with an empty DeviceInfo over Wi-Fi: assume the standard set (as libgphoto2 does)
        props = [0x5001, 0x5005, 0x5007, 0x500A, 0x500D, 0x500E, 0x500F, 0x5010, 0x5013]
        return {"manufacturer": "FUJIFILM", "model": getattr(self.client, "camera_name", "") or "X camera",
                "operations": [f"0x{c:04X}" for c in (0x1001, 0x1002, 0x1003, 0x1007, 0x1008, 0x1009, 0x100E,
                                                       0x1014, 0x1015, 0x1016, 0x101C)],
                "device_properties": [f"0x{c:04X}" for c in props]}

    def _after_open(self) -> None:
        if not isinstance(self.client, FujiPTPIPClient):
            return
        self.client.transaction(OC_SET_DEVICE_PROP_VALUE, [DP_FUJI_INIT_SEQUENCE], data_out=struct.pack("<H", 5))
        r = self.client.transaction(OC_GET_DEVICE_PROP_VALUE, [DP_FUJI_APP_VERSION])
        if r.ok and len(r.data) >= 4:
            self.client.transaction(OC_SET_DEVICE_PROP_VALUE, [DP_FUJI_APP_VERSION], data_out=r.data[:4])
        self.client.transaction(OC_INITIATE_OPEN_CAPTURE, [0, 0])
        self.client.open_streams()

    def live_view(self) -> bytes:
        c = self.client
        if not isinstance(c, FujiPTPIPClient) or c.jpg is None:
            return super().live_view()
        if self.stream is None or not self.stream.alive:
            def reader(stop, put):
                while not stop.is_set():
                    jpg = c.read_jpeg()
                    if jpg:
                        put(jpg)
            self.stream = FrameStream(reader, name="fuji-live")
        return self.stream.latest()

    def movie_record(self, start: bool) -> None:
        if not isinstance(self.client, FujiPTPIPClient):
            return super().movie_record(start)
        r = self.client.transaction(OC_FUJI_MOVIE_START if start else OC_FUJI_MOVIE_STOP)
        if not r.ok:
            raise Unsupported("This Fujifilm camera does not start video from the PC (switch it to movie mode).")
        self.recording = start

    def close(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream = None
        super().close()

    def capabilities(self) -> dict:
        d = super().capabilities()
        if isinstance(self.client, FujiPTPIPClient):
            d.update(live_view=self.client.jpg is not None, movie=True)
        return d
