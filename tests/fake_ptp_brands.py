"""Simulated Canon EOS and Nikon cameras at the PTP transaction level (INVENTED test data, not recorded from real
cameras). They answer the requests the Canon and Nikon drivers send, the way libgphoto2's code says real bodies do,
so the drivers' parsing, value conversion and flow can be tested without hardware. Passing these tests proves the
drivers are self-consistent, not that a real Canon or Nikon behaves like this.
"""
from __future__ import annotations

import io
import struct

from camctl.ptpip import Response

OK, BUSY = 0x2001, 0x2019


def _s(text: str) -> bytes:
    if not text:
        return b"\x00"
    t = text + "\x00"
    return bytes([len(t)]) + t.encode("utf-16-le")


def _a(xs) -> bytes:
    return struct.pack("<I", len(xs)) + b"".join(struct.pack("<H", x) for x in xs)


def device_info(ext_id: int, ext_desc: str, ops, props, maker: str, model: str, fw: str = "1.0.0") -> bytes:
    return (struct.pack("<HIH", 100, ext_id, 100) + _s(ext_desc) + struct.pack("<H", 0) + _a(ops) + _a([0x4002])
            + _a(props) + _a([0x3801]) + _a([0x3801]) + _s(maker) + _s(model) + _s(fw) + _s("0000123"))


def jpeg(w: int = 320, h: int = 180, label: str = "") -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), (70, 80, 95))
    d = ImageDraw.Draw(im)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=(60 + y * 60 // h, 70 + y * 40 // h, 90))
    d.ellipse([w * 0.4, h * 0.2, w * 0.6, h * 0.75], fill=(190, 150, 125))
    if label:
        d.text((8, 8), label, fill=(255, 255, 255))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


class FakeCanon:
    """An EOS body: settings only through events, live view after EVF on, files to the PC after a release."""
    camera_name = ""

    OPS = [0x1001, 0x1002, 0x1003, 0x1009, 0x9109, 0x9110, 0x9114, 0x9115, 0x9116, 0x9117, 0x9128, 0x9129, 0x9153,
           0x9154, 0x9155, 0x9159, 0x9160]

    def __init__(self, model: str = "Canon EOS R50 (simulated)"):
        self.model = model
        self.values = {0xD101: 0x30, 0xD102: 0x70, 0xD103: 0x58, 0xD104: 0x00, 0xD105: 3, 0xD108: 0, 0xD109: 0,
                       0xD10A: 5200, 0xD111: 3, 0xD11C: 1, 0xD1B0: 1, 0xD1B1: 0, 0xD1B8: 0}
        self.lists = {0xD101: [0x20, 0x23, 0x25, 0x28, 0x2B, 0x2D, 0x30, 0x33, 0x35, 0x38, 0x40, 0x48, 0x50],
                      0xD102: [0x0C, 0x10, 0x38, 0x48, 0x60, 0x65, 0x68, 0x70, 0x78, 0x80, 0x98],
                      0xD103: [0x00, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70, 0x78],
                      0xD104: [0xF8, 0xFB, 0xFD, 0x00, 0x03, 0x05, 0x08],
                      0xD109: [0, 1, 2, 3, 4, 5, 8, 9]}
        self.queue: list[bytes] = []
        self.sent_all = False
        self.remote = False
        self.files: dict[int, bytes] = {}
        self.log: list[tuple] = []
        self.lv_misses = 1
        self.connected = False

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    @staticmethod
    def _rec(code: int, body: bytes) -> bytes:
        return struct.pack("<II", 8 + len(body), code) + body

    def _all(self) -> bytes:
        out = b""
        for p, vs in self.lists.items():
            out += self._rec(0xC18A, struct.pack("<III", p, 3, len(vs)) + b"".join(struct.pack("<I", v) for v in vs))
        for p, v in self.values.items():
            out += self._rec(0xC189, struct.pack("<II", p, v))
        return out

    def transaction(self, op, params=(), data_out=None):
        self.log.append((op, tuple(params), data_out))
        if op == 0x1001:
            return Response(OK, [], device_info(0x0000000B, "canon.com", self.OPS, [], "Canon Inc.", self.model))
        if op in (0x1002, 0x1003):
            return Response(OK, [], b"")
        if op == 0x9114:
            self.remote = bool(params[0])
            return Response(OK, [], b"")
        if op == 0x9115:
            return Response(OK, [], b"")
        if op == 0x9116:
            data = b""
            if not self.sent_all:
                data, self.sent_all = self._all(), True
            data += b"".join(self.queue)
            self.queue.clear()
            return Response(OK, [], data + struct.pack("<II", 8, 0))
        if op == 0x9110:
            _size, prop, val = struct.unpack("<III", data_out[:12])
            if prop in self.lists and val not in self.lists[prop]:
                return Response(0x201C, [], b"")                       # invalid value
            self.values[prop] = val
            self.queue.append(self._rec(0xC189, struct.pack("<II", prop, val)))
            return Response(OK, [], b"")
        if op == 0x9153:
            if self.values[0xD1B1] != 1 or not self.values[0xD1B0] & 2:
                return Response(0xA102, [], b"")
            if self.lv_misses:
                self.lv_misses -= 1
                return Response(0xA102, [], b"")
            j = jpeg(label="Canon (simulated)")
            return Response(OK, [], struct.pack("<II", 8 + len(j), 1) + j + struct.pack("<II", 16, 17) + b"\x00" * 8)
        if op == 0x9128:
            return Response(OK, [], b"")
        if op == 0x9129:
            if self.values[0xD11C] == 4:
                for h, fmt, data in ((0x9001, 0xB108, b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 64), (0x9002, 0x3801, jpeg(64, 48))):
                    self.files[h] = data
                    body = struct.pack("<IIH", h, 0x00010001, fmt) + b"\x00" * 10 + struct.pack("<I", len(data)) \
                        + struct.pack("<I", 0) + b"\x00" * 4 + b"IMG_0001\x00"
                    self.queue.append(self._rec(0xC181, body))
            return Response(OK, [], b"")
        if op == 0x1009:
            return Response(OK, [], self.files.pop(params[0], b""))
        if op in (0x9117, 0x9154, 0x9155, 0x9159, 0x9160):
            return Response(OK, [], b"")
        return Response(0x2005, [], b"")                                # operation not supported


class FakeNikon:
    """A Nikon Z body: standard properties, Nikon shutter 0xD100, live view with a header, capture into SDRAM."""
    camera_name = ""
    OPS = [0x1001, 0x1002, 0x1003, 0x1007, 0x1008, 0x1009, 0x100E, 0x1014, 0x1015, 0x1016, 0x90C0, 0x90C1,
           0x90C7, 0x90C8, 0x9201, 0x9202, 0x9203, 0x9204, 0x9205, 0x920A, 0x920B]
    PROPS = [0x5001, 0x5005, 0x5007, 0x500A, 0x500D, 0x500E, 0x500F, 0x5010, 0xD100]

    def __init__(self, model: str = "Z 50 (simulated)"):
        self.model = model
        # code: (datatype, writable, value, form, list-or-range)
        self.p = {
            0x5001: [2, 0, 80, 1, (0, 100, 1)],
            0x5005: [4, 1, 2, 2, [2, 4, 5, 6, 7, 0x8010, 0x8011, 0x8012]],
            0x5007: [4, 1, 560, 2, [350, 400, 450, 500, 560, 630, 710, 800, 1100, 1600, 2200]],
            0x500A: [4, 1, 0x8010, 2, [1, 0x8010, 0x8011, 0x8012]],
            0x500D: [6, 1, 80, 2, [10000, 1250, 80, 40]],
            0x500E: [4, 1, 1, 2, [1, 2, 3, 4]],
            0x500F: [4, 1, 400, 2, [100, 200, 400, 800, 1600, 3200, 6400, 12800]],
            0x5010: [3, 1, 0, 2, [-1000, -667, -333, 0, 333, 667, 1000]],
            0xD100: [6, 1, (1 << 16) | 125, 2, [(1 << 16) | d for d in (4000, 1000, 250, 125, 60, 50, 30)]
                     + [(1 << 16) | 1, (13 << 16) | 10, 0xFFFFFFFF]],
        }
        self.events: list[tuple[int, int]] = []
        self.files: dict[int, bytes] = {}
        self.lv = False
        self.busy = 1
        self.recording = False
        self.log: list[tuple] = []

    def connect(self):
        pass

    def close(self):
        pass

    def _desc(self, code):
        dt, w, v, form, f = self.p[code]
        fmt = "<" + {2: "B", 3: "h", 4: "H", 6: "I"}[dt]
        out = struct.pack("<HHB", code, dt, w) + struct.pack(fmt, v) + struct.pack(fmt, v) + struct.pack("<B", form)
        if form == 1:
            out += b"".join(struct.pack(fmt, x) for x in f)
        else:
            out += struct.pack("<H", len(f)) + b"".join(struct.pack(fmt, x) for x in f)
        return out

    def transaction(self, op, params=(), data_out=None):
        self.log.append((op, tuple(params), data_out))
        if op == 0x1001:
            return Response(OK, [], device_info(0x0000000A, "microsoft.com: 1.0", self.OPS, self.PROPS,
                                                 "Nikon Corporation", self.model))
        if op in (0x1002, 0x1003):
            return Response(OK, [], b"")
        if op == 0x1014:
            if params[0] not in self.p:
                return Response(0x200A, [], b"")
            return Response(OK, [], self._desc(params[0]))
        if op == 0x1016:
            code = params[0]
            dt, w, _v, _form, f = self.p[code]
            val = struct.unpack("<" + {2: "B", 3: "h", 4: "H", 6: "I"}[dt], data_out)[0]
            if not w or (isinstance(f, list) and val not in f):
                return Response(0x201C, [], b"")
            self.p[code][2] = val
            return Response(OK, [], b"")
        if op == 0x90C8:
            if self.busy:
                self.busy -= 1
                return Response(BUSY, [], b"")
            return Response(OK, [], b"")
        if op == 0x9201:
            self.lv = True
            self.busy = 1
            return Response(OK, [], b"")
        if op == 0x9202:
            self.lv = False
            return Response(OK, [], b"")
        if op == 0x9203:
            if not self.lv:
                return Response(0xA00B, [], b"")
            return Response(OK, [], b"\x00" * 384 + jpeg(640, 424, "Nikon (simulated)"))
        if op == 0x90C0:
            self.files[0xFFFF0001] = jpeg(64, 48)
            self.events += [(0xC101, 0xFFFF0001), (0xC102, 0)]
            return Response(OK, [], b"")
        if op == 0x90C7:
            ev, self.events = self.events, []
            return Response(OK, [], struct.pack("<H", len(ev)) + b"".join(struct.pack("<HI", c, p) for c, p in ev))
        if op == 0x1008:
            return Response(OK, [], struct.pack("<IH", 0x00010001, 0x3801) + b"\x00" * 40)
        if op == 0x1009:
            return Response(OK, [], self.files.pop(params[0], b""))
        if op == 0x920A:
            if not self.lv:
                return Response(0xA00B, [], b"")
            self.recording = True
            return Response(OK, [], b"")
        if op == 0x920B:
            self.recording = False
            return Response(OK, [], b"")
        if op in (0x90C1, 0x9204, 0x9205):
            self.busy = 1
            return Response(OK, [], b"")
        return Response(0x2005, [], b"")


class FakePlainPTP(FakeNikon):
    """Another maker with plain PTP only: standard properties and InitiateCapture, nothing else."""
    OPS = [0x1001, 0x1002, 0x1003, 0x1007, 0x1008, 0x1009, 0x100E, 0x1014, 0x1015, 0x1016]
    PROPS = [0x5001, 0x5007, 0x500D, 0x500F]

    def __init__(self):
        super().__init__("X-S10 (simulated)")
        self.handles: list[int] = [1, 2]

    def transaction(self, op, params=(), data_out=None):
        if op == 0x1001:
            return Response(OK, [], device_info(0x00000006, "", self.OPS, self.PROPS, "FUJIFILM", self.model))
        if op == 0x1007:
            return Response(OK, [], struct.pack("<I", len(self.handles)) + b"".join(struct.pack("<I", h) for h in self.handles))
        if op == 0x100E:
            self.handles.append(3)
            self.files[3] = jpeg(64, 48)
            return Response(OK, [], b"")
        if op in (0x90C0, 0x9203, 0x90C7, 0x90C8):
            return Response(0x2005, [], b"")
        return super().transaction(op, params, data_out)
