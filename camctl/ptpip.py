"""Minimal PTP/IP (ISO 15740 over TCP) client.

PTP/IP uses two TCP connections to port 15740: a command channel for
operations and an event channel for asynchronous camera events.
"""
from __future__ import annotations

import socket
import struct
import threading
import uuid
from dataclasses import dataclass, field

PTPIP_PORT = 15740

# Packet types
INIT_COMMAND_REQUEST = 1
INIT_COMMAND_ACK = 2
INIT_EVENT_REQUEST = 3
INIT_EVENT_ACK = 4
INIT_FAIL = 5
SIGN_IN_TEXT = 'This camera model probably needs a sign-in (access authentication with user name, password and fingerprint) that Studio does not support yet. The USB cable may work instead.'
OPERATION_REQUEST = 6
OPERATION_RESPONSE = 7
EVENT = 8
START_DATA = 9
DATA = 10
CANCEL = 11
END_DATA = 12
PROBE_REQUEST = 13
PROBE_RESPONSE = 14

# Data phase info in OperationRequest
DATA_NONE_OR_IN = 1
DATA_OUT = 2

RC_OK = 0x2001


class PTPError(Exception):
    pass


@dataclass
class Response:
    code: int
    params: list[int] = field(default_factory=list)
    data: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code == RC_OK


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PTPError("connection closed by camera")
        buf += chunk
    return bytes(buf)


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    length, ptype = struct.unpack("<II", _recv_exact(sock, 8))
    if length < 8:
        raise PTPError(f"bad packet length {length}")
    return ptype, _recv_exact(sock, length - 8)


def build_packet(ptype: int, payload: bytes = b"") -> bytes:
    return struct.pack("<II", 8 + len(payload), ptype) + payload


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class PTPIPClient:
    def __init__(self, host: str, port: int = PTPIP_PORT, name: str = "camctl",
                 guid: bytes | None = None, timeout: float = 10.0):
        self.host, self.port, self.name = host, port, name
        self.guid = guid or uuid.uuid4().bytes
        self.timeout = timeout
        self.cmd: socket.socket | None = None
        self.evt: socket.socket | None = None
        self.conn_number = 0
        self.camera_name = ""
        self._tid = 0
        self._lock = threading.Lock()
        self.events: list[tuple[int, list[int]]] = []

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        try:
            self.cmd = socket.create_connection((self.host, self.port), self.timeout)
        except ConnectionRefusedError:
            # no plain PTP/IP here; newer Sony bodies with access authentication answer on SSH instead
            try:
                socket.create_connection((self.host, 22), 1.5).close()
                raise PTPError(SIGN_IN_TEXT) from None
            except OSError:
                raise PTPError(f"The camera at {self.host} does not accept PC Remote connections right now.") from None
        self.cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        payload = self.guid + _utf16z(self.name) + struct.pack("<I", 0x00010000)
        self.cmd.sendall(build_packet(INIT_COMMAND_REQUEST, payload))
        ptype, body = read_packet(self.cmd)        # waits while the camera shows its pairing prompt
        if ptype == INIT_FAIL:
            reason = struct.unpack("<I", body[:4])[0] if len(body) >= 4 else 0
            self.close()
            raise PTPError({1: "The camera refused the connection. Usually it is still holding the previous one "
                               "(after a Wi-Fi drop or low battery): switch the camera off and on, Studio reconnects "
                               "by itself. If it keeps refusing after that, pair again: MENU > Network > PC Remote "
                               "Function > Pairing, then confirm on the camera.",
                            2: "The camera is busy with another app or computer."}.get(
                reason, f"The camera refused the connection (reason 0x{reason:x}). " + SIGN_IN_TEXT))
        if ptype != INIT_COMMAND_ACK:
            raise PTPError(f"unexpected packet {ptype} during init")
        self.conn_number = struct.unpack("<I", body[:4])[0]
        self.camera_name = body[20:].decode("utf-16-le", "ignore").split("\x00")[0]
        self.cmd.settimeout(min(self.timeout, 15.0))   # long wait only for the pairing prompt

        self.evt = socket.create_connection((self.host, self.port), self.timeout)
        self.evt.sendall(build_packet(INIT_EVENT_REQUEST, struct.pack("<I", self.conn_number)))
        ptype, _ = read_packet(self.evt)
        if ptype != INIT_EVENT_ACK:
            raise PTPError(f"event channel init failed (packet {ptype})")
        threading.Thread(target=self._event_loop, daemon=True).start()

    def _event_loop(self) -> None:
        try:
            self.evt.settimeout(None)
            while True:
                ptype, body = read_packet(self.evt)
                if ptype == EVENT and len(body) >= 6:
                    code, _tid = struct.unpack("<HI", body[:6])
                    params = list(struct.unpack(f"<{(len(body) - 6) // 4}I", body[6:6 + (len(body) - 6) // 4 * 4]))
                    self.events.append((code, params))
                    del self.events[:-200]
                elif ptype == PROBE_REQUEST:
                    self.evt.sendall(build_packet(PROBE_RESPONSE))
        except (OSError, PTPError):
            pass

    def close(self) -> None:
        for s in (self.cmd, self.evt):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.cmd = self.evt = None

    # -- transactions -----------------------------------------------------
    def transaction(self, opcode: int, params: list[int] | tuple = (),
                    data_out: bytes | None = None) -> Response:
        if self.cmd is None:
            raise PTPError("not connected")
        with self._lock:
            self._tid += 1
            tid = self._tid
            phase = DATA_OUT if data_out is not None else DATA_NONE_OR_IN
            req = struct.pack("<IHI", phase, opcode, tid) + b"".join(
                struct.pack("<I", p & 0xFFFFFFFF) for p in params)
            self.cmd.sendall(build_packet(OPERATION_REQUEST, req))
            if data_out is not None:
                self.cmd.sendall(build_packet(START_DATA, struct.pack("<IQ", tid, len(data_out))))
                self.cmd.sendall(build_packet(END_DATA, struct.pack("<I", tid) + data_out))

            data = bytearray()
            while True:
                ptype, body = read_packet(self.cmd)
                if ptype in (START_DATA, PROBE_REQUEST):
                    if ptype == PROBE_REQUEST:
                        self.cmd.sendall(build_packet(PROBE_RESPONSE))
                    continue
                if ptype in (DATA, END_DATA):
                    data += body[4:]
                    continue
                if ptype == OPERATION_RESPONSE:
                    code, rtid = struct.unpack("<HI", body[:6])
                    rest = body[6:]
                    rparams = list(struct.unpack(f"<{len(rest) // 4}I", rest[:len(rest) // 4 * 4]))
                    return Response(code, rparams, bytes(data))
                if ptype == CANCEL:
                    raise PTPError("camera cancelled the transaction")
                # ignore anything else
