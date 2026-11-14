"""PTP over USB (still-image class) transport with the same API as PTPIPClient.

Needs pyusb + a libusb backend. On Windows the camera must use the WinUSB
driver (install it once with Zadig for the camera's 'PTP' interface).
"""
from __future__ import annotations

import struct
import threading

from .ptpip import PTPError, Response

SONY_VID = 0x054C

CT_COMMAND, CT_DATA, CT_RESPONSE, CT_EVENT = 1, 2, 3, 4
CHUNK = 1024 * 1024


def _backend():
    try:
        import libusb_package  # bundles libusb-1.0.dll on Windows
        return libusb_package.get_libusb1_backend()
    except ImportError:
        return None


def find_ptp_cameras(vendor: int | set | frozenset | None = None) -> list:
    """PTP cameras on the bus. `vendor` is one USB vendor id or a set of them; other makers' devices are skipped
    before their descriptors are read (reading every device on the bus is slow on Windows)."""
    import usb.core
    vendors = None if vendor is None else ({vendor} if isinstance(vendor, int) else set(vendor))
    found = []
    for dev in usb.core.find(find_all=True, backend=_backend()):
        if vendors is not None and dev.idVendor not in vendors:
            continue
        try:
            for cfg in dev:
                for intf in cfg:
                    if intf.bInterfaceClass == 6:  # Still Image (PTP)
                        found.append(dev)
                        raise StopIteration
        except StopIteration:
            continue
        except Exception:  # noqa: BLE001 - devices we cannot inspect
            continue
    return found


class PTPUSBClient:
    def __init__(self, vendor: int | None = SONY_VID, timeout_ms: int = 10000):
        self.vendor = vendor
        self.timeout = timeout_ms
        self.dev = self.ep_in = self.ep_out = None
        self.camera_name = ""
        self._tid = 0
        self._lock = threading.Lock()

    def connect(self) -> None:
        import usb.core
        import usb.util
        try:
            devs = find_ptp_cameras(self.vendor)
        except usb.core.NoBackendError as e:
            raise PTPError("libusb not found: run 'pip install libusb-package'") from e
        if not devs:
            raise PTPError("No PTP camera found on USB. Is it plugged in, set to "
                           "'PC Remote', and using the WinUSB driver (Zadig)?")
        dev = devs[0]
        cfg = dev.get_active_configuration() if dev.get_active_configuration() else None
        if cfg is None:
            dev.set_configuration()
            cfg = dev.get_active_configuration()
        intf = next(i for i in cfg if i.bInterfaceClass == 6)
        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except usb.core.USBError as e:
            usb.util.dispose_resources(dev)
            raise PTPError(f"The camera is busy (another program or a stuck Studio holds it): {e}") from e
        eps = list(intf)
        bulk = [e for e in eps if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        self.ep_out = next(e for e in bulk if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = next(e for e in bulk if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.dev = dev
        self.intf = intf.bInterfaceNumber
        try:
            self.camera_name = usb.util.get_string(dev, dev.iProduct) or ""
        except Exception:  # noqa: BLE001
            self.camera_name = ""
        self._clear_halts()
        self.drain()

    def _clear_halts(self) -> None:
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep.bEndpointAddress)
            except Exception:  # noqa: BLE001 - not stalled / not supported
                pass

    def drain(self, max_reads: int = 64) -> int:
        """Throw away data a killed earlier run left in the camera's send buffer."""
        n = 0
        for _ in range(max_reads):
            try:
                n += len(self.dev.read(self.ep_in.bEndpointAddress, CHUNK, 60))
            except Exception:  # noqa: BLE001 - timeout = nothing left
                break
        return n

    def ptp_reset(self) -> None:
        """PTP still-image class 'Device Reset' request: makes a camera that is stuck
        mid-transaction (e.g. after Studio was killed) answer again."""
        if self.dev is None:
            return
        try:
            self.dev.ctrl_transfer(0x21, 0x66, 0, self.intf, None, 3000)
        except Exception:  # noqa: BLE001
            pass
        self._clear_halts()
        self.drain()
        self._tid = 0

    def usb_reset(self) -> None:
        """Re-enumerate the USB device (stronger than ptp_reset). Closes the client."""
        dev = self.dev
        self.close()
        if dev is not None:
            try:
                dev.reset()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self.dev is not None:
            import usb.util
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:  # noqa: BLE001
                pass
        self.dev = None

    def _read_container(self) -> tuple[int, int, int, bytes]:
        buf = bytes(self.dev.read(self.ep_in.bEndpointAddress, CHUNK, self.timeout))
        while len(buf) < 12:  # skip zero-length packets
            buf += bytes(self.dev.read(self.ep_in.bEndpointAddress, CHUNK, self.timeout))
        length, ctype, code, tid = struct.unpack("<IHHI", buf[:12])
        while len(buf) < length:
            buf += bytes(self.dev.read(self.ep_in.bEndpointAddress, CHUNK, self.timeout))
        return ctype, code, tid, buf[12:length]

    def transaction(self, opcode: int, params: list[int] | tuple = (),
                    data_out: bytes | None = None) -> Response:
        if self.dev is None:
            raise PTPError("not connected")
        with self._lock:
            self._tid += 1
            tid = self._tid
            p = b"".join(struct.pack("<I", x & 0xFFFFFFFF) for x in params)
            self.dev.write(self.ep_out.bEndpointAddress,
                           struct.pack("<IHHI", 12 + len(p), CT_COMMAND, opcode, tid) + p, self.timeout)
            if data_out is not None:
                self.dev.write(self.ep_out.bEndpointAddress,
                               struct.pack("<IHHI", 12 + len(data_out), CT_DATA, opcode, tid) + data_out,
                               self.timeout)
            data = b""
            while True:
                ctype, code, _tid, payload = self._read_container()
                if ctype == CT_DATA:
                    data = payload
                elif ctype == CT_RESPONSE:
                    rparams = list(struct.unpack(f"<{len(payload) // 4}I", payload[:len(payload) // 4 * 4]))
                    return Response(code, rparams, data)



def sony_without_driver() -> list[dict]:
    """Sony cameras Windows sees on USB that Studio cannot open (no WinUSB driver): read-only, nothing installed.
    A camera that is off and only charging shows up as a different device and is not listed here."""
    import os
    import subprocess
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["pnputil", "/enum-devices", "/connected"], capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return []
    found = []
    for block in out.replace("\r", "").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        inst = fields.get("instance id", "")
        if "VID_054C" not in inst.upper() or "&MI_" in inst.upper():
            continue
        cls = fields.get("class name", "")
        drv = fields.get("driver name", "").lower()
        if cls in ("WPD", "Image", "Camera", "USB") and "winusb" not in drv:
            found.append({"instance": inst, "name": fields.get("device description", "Sony camera"), "class": cls})
    return found


def cameras_on_usb() -> list[dict]:
    """Every camera Windows sees on USB, by maker (read-only, nothing installed): brand, vendor and product id, name,
    device class. Used to tell a user with another brand plainly that it is not supported yet."""
    import os
    import re
    import subprocess
    from .drivers.base import BRANDS
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["pnputil", "/enum-devices", "/connected"], capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return []
    found = []
    for block in out.replace("\r", "").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        inst = fields.get("instance id", "")
        m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", inst.upper())
        if not m or "&MI_" in inst.upper():
            continue
        vid, pid = int(m[1], 16), int(m[2], 16)
        cls = fields.get("class name", "")
        if vid in BRANDS and cls in ("WPD", "Image", "Camera", "USB"):
            found.append({"brand": BRANDS[vid], "vendor_id": f"0x{vid:04X}", "product_id": f"0x{pid:04X}",
                          "name": fields.get("device description", ""), "class": cls})
    return found
