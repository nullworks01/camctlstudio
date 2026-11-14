"""Wi-Fi connection: Sony cameras in PC Remote 'Access Point' mode (camera on the home Wi-Fi), and the other
makers' Wi-Fi routes (Canon / Nikon PTP/IP, Fujifilm PC Shoot, and the HTTP cameras that are their own access point:
Panasonic LUMIX, OM System / Olympus, Ricoh / Pentax). The other makers are UNTESTED ON REAL CAMERAS.

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
FUJI_PORT = 55740
# cameras that are their own access point, and how to recognise them (path, word in the answer)
AP_CAMERAS = [("Panasonic", "192.168.54.1", "cam.cgi?mode=getstate", "<camrply"),
              ("OM System / Olympus", "192.168.0.10", "get_caminfo.cgi", "<model"),
              ("Ricoh / Pentax", "192.168.0.1", "v1/props", '"model"')]
SSDP_MAKERS = [("Sony", ("sony", "ilce", "zv-")), ("Canon", ("canon",)), ("Nikon", ("nikon",)),
               ("Panasonic", ("panasonic", "lumix")), ("Fujifilm", ("fujifilm",))]
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


def brand_for(host: str | None) -> str | None:
    """The maker he picked for this camera address (None = Sony, as before)."""
    if not host or host == "usb":
        return None
    return (load().get("brands") or {}).get(split(host)[0])


def remember_brand(host: str, brand: str | None) -> None:
    brands = dict(load().get("brands") or {})
    ip = split(host)[0]
    if brand and brand != "Sony":
        brands[ip] = brand
    else:
        brands.pop(ip, None)
    save(brands=brands)


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
    """UPnP devices that look like cameras: [{ip, name, model, udn, manufacturer, brand}]."""
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
        brand = next((b for b, words in SSDP_MAKERS if any(w in blob for w in words)), None)
        if brand:
            out.append({"ip": ip, "name": info.get("friendlyName", ""), "model": info.get("modelName", ""),
                        "udn": info.get("UDN", ""), "manufacturer": info.get("manufacturer", ""), "via": "ssdp",
                        "brand": brand})
    return out


def _describe(location: str) -> dict:
    try:
        with urllib.request.urlopen(location, timeout=1.5) as r:
            xml = r.read(65536).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return {}
    return {k: m.group(1).strip() for k in ("friendlyName", "modelName", "manufacturer", "UDN")
            if (m := re.search(rf"<{k}>([^<]*)</{k}>", xml))}


def _http_probe(ip: str, path: str, word: str, timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(f"http://{ip}/{path}", headers={"User-Agent": "OI.Share v2", "Host": ip})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return word in r.read(8192).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return False


def access_point_cameras(subnets: list[str] | None = None) -> list[dict]:
    """Cameras that are their own Wi-Fi access point, when this PC is on that network (asks only those IPs)."""
    nets = set(subnets if subnets is not None else local_subnets())
    out = []
    for brand, ip, path, word in AP_CAMERAS:
        if ip.rsplit(".", 1)[0] in nets and _http_probe(ip, path, word):
            out.append({"ip": ip, "host": f"{ip}:80", "name": "", "model": "", "udn": "", "via": "camera network",
                        "brand": brand, "usable": True})
    return out


def discover(scan_lan: bool = True) -> list[dict]:
    """Cameras on the network: SSDP first, the cameras that are their own access point, then a port scan of the
    local /24 (PTP/IP 15740 for Sony / Canon / Nikon, 55740 for Fujifilm). Each hit says its maker (brand, None
    when only the PTP/IP port answered: Sony is assumed, as before) and the address to use (host)."""
    cams: dict[str, dict] = {}
    for c in ssdp():
        c.setdefault("brand", "Sony")
        if c["brand"] == "Panasonic":
            c["host"], c["usable"] = f"{c['ip']}:80", True
            c["ptpip"] = False
        elif c["brand"] == "Fujifilm":
            c["usable"] = _port_open(c["ip"], FUJI_PORT, 0.6)
            c["host"], c["ptpip"] = f"{c['ip']}:{FUJI_PORT}", False
        else:
            c["ptpip"] = c["usable"] = _port_open(c["ip"], PTPIP_PORT, 0.6)
            c["host"] = c["ip"]
        cams[c["ip"]] = c
    for c in access_point_cameras():
        cams.setdefault(c["ip"], c)
    if scan_lan:
        for ip in scan(skip=set(cams)):
            cams[ip] = {"ip": ip, "host": ip, "name": "", "model": "", "udn": "", "via": "port scan", "ptpip": True,
                        "usable": True, "brand": None}
        for ip in scan(port=FUJI_PORT, skip=set(cams)):
            cams[ip] = {"ip": ip, "host": f"{ip}:{FUJI_PORT}", "name": "", "model": "", "udn": "", "via": "port scan",
                        "ptpip": False, "usable": True, "brand": "Fujifilm"}
    cfg = load()
    remembered, brands = cfg.get("host"), cfg.get("brands") or {}
    for c in cams.values():
        c.setdefault("ptpip", False)
        c.setdefault("usable", bool(c.get("ptpip")))
        c["remembered"] = remembered is not None and split(c["host"])[0] == split(remembered)[0]
        if c.get("brand") is None and c["ip"] in brands:
            c["brand"] = brands[c["ip"]]
    return sorted(cams.values(), key=lambda c: (not c.get("usable"), c["via"] == "port scan", c["ip"]))


def split(host: str) -> tuple[str, int]:
    """'10.0.0.38' or '10.0.0.38:15740' -> (ip, port)."""
    ip, _, port = host.partition(":")
    return ip, int(port) if port.isdigit() else PTPIP_PORT


def reachable(host: str, timeout: float = 0.6) -> bool:
    return _port_open(*split(host), timeout)
