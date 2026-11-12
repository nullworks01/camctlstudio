"""Wi-Fi connection for Sony cameras in PC Remote 'Access Point' mode (camera on the home Wi-Fi).

- A stable GUID for this PC: Sony cameras pair with the GUID sent in the PTP/IP Init
  Command Request, so the same GUID must be used every time (else it asks again).
- Discovery: SSDP (UPnP M-SEARCH, Sony answers with its device description) plus a
  scan of the local /24 for the PTP/IP port 15740.
- Remembers the paired camera (IP, name, UDN) in %APPDATA%\\camctl\\wifi.json.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import socket
import time
import urllib.request
import uuid
from pathlib import Path

PTPIP_PORT = 15740
BASE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl"
CONFIG = BASE / "wifi.json"


BACKUP_NAME = "wifi.json.bak"


def _read(path: Path) -> dict | None:
    for _ in range(4):                        # a reader can meet a file mid-write / locked by antivirus
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.15)
    return None


def load() -> dict:
    """The saved connection (IP, pairing GUID, last mode). Never silently empty when a copy exists:
    falls back to the last good backup, so a start-up read hiccup can't switch Studio to USB."""
    cfg = _read(CONFIG)
    if cfg is None:
        cfg = _read(CONFIG.with_name(BACKUP_NAME))
    return cfg or {}


def save(**changes) -> dict:
    cfg = load()
    cfg.update(changes)
    BASE.mkdir(parents=True, exist_ok=True)
    data = json.dumps(cfg, indent=1)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, CONFIG)                   # atomic: nobody ever reads half a file
    try:
        CONFIG.with_name(BACKUP_NAME).write_text(data, encoding="utf-8")
    except OSError:
        pass
    return cfg


def guid() -> bytes:
    """This PC's PTP/IP GUID (created once, then reused so pairing sticks)."""
    cfg = load()
    g = cfg.get("guid")
    if not (isinstance(g, str) and re.fullmatch(r"[0-9a-f]{32}", g)):
        g = uuid.uuid4().hex
        save(guid=g)
    return bytes.fromhex(g)


def client_name() -> str:
    """Name shown on the camera's pairing prompt."""
    return f"camctl Studio ({socket.gethostname()[:20]})"


# ---------------------------------------------------------------- discovery
def local_subnets() -> list[str]:
    """'a.b.c' prefixes of this PC's private IPv4 networks (no packets are sent)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))        # picks the default interface; nothing is sent
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    priv = [ip for ip in ips if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", ip)]
    return sorted({ip.rsplit(".", 1)[0] for ip in priv})


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(subnets: list[str] | None = None, port: int = PTPIP_PORT, timeout: float = 0.35,
         skip: set[str] | None = None) -> list[str]:
    """IPs on the local /24 networks that accept TCP on the PTP/IP port."""
    targets = [f"{p}.{i}" for p in (subnets or local_subnets()) for i in range(1, 255)]
    targets = [t for t in targets if t not in (skip or set())]
    with cf.ThreadPoolExecutor(max_workers=96) as ex:
        hits = [ip for ip, ok in zip(targets, ex.map(lambda ip: _port_open(ip, port, timeout), targets)) if ok]
    return hits


def ssdp(timeout: float = 2.0) -> list[dict]:
    """UPnP devices that look like Sony cameras: [{ip, name, model, udn, location}]."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    found: dict[str, dict] = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _p) = s.recvfrom(4096)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "ignore")
            loc = re.search(r"(?im)^location:\s*(\S+)", text)
            if ip not in found or (loc and not found[ip].get("location")):
                found[ip] = {"ip": ip, "location": loc.group(1) if loc else "", "raw": text}
        s.close()
    except OSError:
        return []
    out = []
    for ip, d in found.items():
        info = _describe(d["location"]) if d["location"] else {}
        blob = (d["raw"] + json.dumps(info)).lower()
        if "sony" in blob or "ilce" in blob or "zv-" in blob:
            out.append({"ip": ip, "name": info.get("friendlyName", ""), "model": info.get("modelName", ""),
                        "udn": info.get("UDN", ""), "manufacturer": info.get("manufacturer", ""), "via": "ssdp"})
    return out


def _describe(location: str) -> dict:
    try:
        with urllib.request.urlopen(location, timeout=1.5) as r:
            xml = r.read(65536).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return {}
    return {k: m.group(1).strip() for k in ("friendlyName", "modelName", "manufacturer", "UDN")
            if (m := re.search(rf"<{k}>([^<]*)</{k}>", xml))}


def discover(scan_lan: bool = True) -> list[dict]:
    """Cameras on the network: SSDP first, then a PTP/IP port scan of the local /24."""
    cams = {c["ip"]: c for c in ssdp()}
    for c in cams.values():
        c["ptpip"] = _port_open(c["ip"], PTPIP_PORT, 0.6)
    if scan_lan:
        for ip in scan(skip=set(cams)):
            cams[ip] = {"ip": ip, "name": "", "model": "", "udn": "", "via": "port scan", "ptpip": True}
    remembered = load().get("host")
    for c in cams.values():
        c["remembered"] = c["ip"] == remembered
    return sorted(cams.values(), key=lambda c: (not c.get("ptpip"), c["via"] != "ssdp", c["ip"]))


def split(host: str) -> tuple[str, int]:
    """'10.0.0.38' or '10.0.0.38:15740' -> (ip, port)."""
    ip, _, port = host.partition(":")
    return ip, int(port) if port.isdigit() else PTPIP_PORT


def reachable(host: str, timeout: float = 0.6) -> bool:
    return _port_open(*split(host), timeout)
