"""A fake Sony camera speaking PTP/IP, for testing without hardware."""
import socket, struct, threading
from camctl import ptpip as P

JPEG = b"\xff\xd8fakejpeg\xff\xd9"
RAW = b"II*\x00fakearw"

def prop_blob(props):
    # props: list of (code, dtype_fmt, dtype, value, choices)
    out = struct.pack("<Q", len(props))
    for code, fmt, dt, val, choices in props:
        out += struct.pack("<HHBB", code, dt, 1, 1) + struct.pack("<" + fmt, val) * 2
        out += struct.pack("<BH", 2, len(choices)) + b"".join(struct.pack("<" + fmt, c) for c in choices)
        out += struct.pack("<H", len(choices)) + b"".join(struct.pack("<" + fmt, c) for c in choices)
    return out

class FakeSony:
    def __init__(self):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.props = {0xD21E: ["I", 6, 100, [100, 200, 400, 0xFFFFFF]],
                      0x5007: ["H", 4, 280, [280, 400, 560]],
                      0xD215: ["H", 4, 0, [0]]}
        self.ops = []
        threading.Thread(target=self.run, daemon=True).start()

    def send(self, s, t, p=b""): s.sendall(P.build_packet(t, p))

    def run(self):
        cmd, _ = self.srv.accept()
        t, body = P.read_packet(cmd); assert t == P.INIT_COMMAND_REQUEST
        self.send(cmd, P.INIT_COMMAND_ACK, struct.pack("<I", 7) + b"\0"*16 + "ZV-E10".encode("utf-16-le") + b"\0\0" + struct.pack("<I", 0x10000))
        evt, _ = self.srv.accept()
        t, body = P.read_packet(evt); assert t == P.INIT_EVENT_REQUEST and struct.unpack("<I", body)[0] == 7
        self.send(evt, P.INIT_EVENT_ACK); self.evt = evt
        try:
            while True:
                t, body = P.read_packet(cmd)
                phase, op, tid = struct.unpack("<IHI", body[:10])
                params = list(struct.unpack(f"<{(len(body)-10)//4}I", body[10:]))
                data_in = None
                if phase == P.DATA_OUT:
                    t1, _ = P.read_packet(cmd); assert t1 == P.START_DATA
                    t2, d = P.read_packet(cmd); assert t2 == P.END_DATA
                    data_in = d[4:]
                self.ops.append((op, params, data_in))
                reply = None
                if op == 0x9209:
                    reply = prop_blob([(c, f, dt, v, ch) for c, (f, dt, v, ch) in self.props.items()])
                elif op == 0x9205:
                    f = self.props[params[0]][0]
                    self.props[params[0]][2] = struct.unpack("<" + f, data_in)[0]
                elif op == 0x9207 and params[0] == 0xD2C2 and struct.unpack("<H", data_in)[0] == 1:
                    # Like a real body in RAW+JPEG: the RAW is queued first, then the JPEG.
                    self.queue = [(0xB101, RAW), (0x3801, JPEG)]
                    self.props[0xD215][2] = 0x8000 | len(self.queue)
                elif op == 0x1008 and params[0] == 0xFFFFC001:
                    fmt = self.queue[0][0] if getattr(self, "queue", None) else 0
                    reply = struct.pack("<IHH", 0x10001, fmt, 0) + b"\0" * 40
                elif op == 0x1009 and params[0] == 0xFFFFC002:
                    reply = struct.pack("<II", 16, len(JPEG)) + b"\0"*8 + JPEG
                elif op == 0x1009:
                    _, reply = self.queue.pop(0)
                    self.props[0xD215][2] = (0x8000 | len(self.queue)) if self.queue else 0
                if reply is not None:
                    self.send(cmd, P.START_DATA, struct.pack("<IQ", tid, len(reply)))
                    self.send(cmd, P.END_DATA, struct.pack("<I", tid) + reply)
                self.send(cmd, P.OPERATION_RESPONSE, struct.pack("<HI", 0x2001, tid))
        except (P.PTPError, OSError):
            pass
