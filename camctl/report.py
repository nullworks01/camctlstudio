"""The "Check this camera" report: what a camera can do with Studio, in two levels, with nothing personal in it.

- summary: app version, brand, model, firmware, connection type, works / does not work per function (fits an issue page)
- full file: what the camera announces about itself (standard PTP DeviceInfo, raw codes in hex), the complete property
  table (code, type, get/set, enabled, value, allowed values or range) and every check Studio ran (what, result, time).

Privacy by allow-list: text values are kept ONLY for model, firmware and lens model; every other text (serial number,
names the owner set, network names, owner / artist / copyright, dates) is replaced by "<text removed>". No IP, no MAC,
no paths, no pictures, no key. Studio never sends a report itself: the user shares it on the issue page if they want.
"""
from __future__ import annotations

import json
import struct
import urllib.parse
from pathlib import Path

APP_VERSION = "2026.11.12"
ISSUE_URL = "https://github.com/nullworks01/camctlstudio/issues/new"     # empty = only "Save report"
REMOVED = "<text removed>"
KEEP_TEXT = {"model", "firmware", "lens_model", "manufacturer", "standard_version", "vendor_extension"}
MAX_URL = 7000


# -- standard PTP DeviceInfo ---------------------------------------------------------------------------------------
def _str(d: bytes, i: int) -> tuple[str, int]:
    n = d[i]
    s = d[i + 1:i + 1 + 2 * n].decode("utf-16-le", "ignore").rstrip("\x00")
    return s, i + 1 + 2 * n


def _arr(d: bytes, i: int) -> tuple[list[int], int]:
    n = struct.unpack_from("<I", d, i)[0]
    return list(struct.unpack_from(f"<{n}H", d, i + 4)), i + 4 + 2 * n


def parse_device_info(d: bytes) -> dict:
    """ISO 15740 DeviceInfo dataset -> dict (codes as hex strings)."""
    std, ext_id, ext_ver = struct.unpack_from("<HIH", d, 0)
    i = 8
    ext_desc, i = _str(d, i)
    mode = struct.unpack_from("<H", d, i)[0]; i += 2
    ops, i = _arr(d, i)
    events, i = _arr(d, i)
    props, i = _arr(d, i)
    capture, i = _arr(d, i)
    image, i = _arr(d, i)
    manufacturer, i = _str(d, i)
    model, i = _str(d, i)
    version, i = _str(d, i)
    serial, i = _str(d, i)
    hx = lambda xs: [f"0x{x:04X}" for x in xs]
    return {"standard_version": f"{std / 100:.2f}", "vendor_extension": f"0x{ext_id:08X} v{ext_ver}",
            "vendor_extension_desc": ext_desc, "functional_mode": mode, "operations": hx(ops), "events": hx(events),
            "device_properties": hx(props), "capture_formats": hx(capture), "image_formats": hx(image),
            "manufacturer": manufacturer, "model": model, "firmware": version, "serial_number": serial}


def _clean_value(key: str, v):
    """The allow-list filter for one value. Hex codes are not personal and stay."""
    import re
    if isinstance(v, str):
        return v if key in KEEP_TEXT or re.fullmatch(r"0x[0-9A-Fa-f]+( v\d+)?", v) else REMOVED
    if isinstance(v, dict):
        return {k: _clean_value(k, x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_value(key, x) for x in v]
    return v


def clean(full: dict) -> dict:
    """Remove everything personal from a full report (allow-list for text; known personal keys dropped)."""
    drop = {"serial_number", "friendly_name", "owner", "artist", "copyright", "ssid", "ip", "mac", "guid", "gps",
            "datetime", "timezone", "paths", "files"}
    out = {}
    for k, v in full.items():
        if k in drop:
            continue
        if k == "properties":
            out[k] = [{**p, "value": p["value"] if not isinstance(p.get("value"), str) else REMOVED,
                       "allowed": [a for a in (p.get("allowed") or []) if not isinstance(a, str)]} for p in v]
        elif k == "checks":
            out[k] = v                                          # Studio's own words and codes, nothing from the owner
        elif k == "device_info":
            out[k] = {kk: _clean_value(kk, vv) for kk, vv in v.items() if kk not in drop}
        else:
            out[k] = _clean_value(k, v)
    return out


def properties_table(props: dict) -> list[dict]:
    rows = []
    for code, p in sorted(props.items()):
        rng = getattr(p, "range", None)
        rows.append({"code": f"0x{code:04X}", "type": f"0x{getattr(p, 'datatype', 0):04X}", "set": bool(getattr(p, "writable", False)),
                     "enabled": bool(getattr(p, "enabled", False)), "value": getattr(p, "value", None),
                     "allowed": list(getattr(p, "choices", None) or []), "range": list(rng) if rng else None})
    return rows


def build(brand: str, device_info: dict | None, props: dict, checks: list[dict], connection: str,
          usb: dict | None = None) -> tuple[dict, str]:
    """(full report already cleaned, summary text). checks: [{name, ok, detail, ms, command}]"""
    di = device_info or {}
    full = {"app_version": APP_VERSION, "brand": brand, "model": di.get("model") or "", "firmware": di.get("firmware") or "",
            "connection": connection, "device_info": di or {"note": "could not be read"},
            "properties": properties_table(props or {}), "checks": checks,
            "usb": {k: v for k, v in (usb or {}).items() if k in ("vendor_id", "product_id", "class")}}
    full = clean(full)
    ok = [c["name"] for c in checks if c.get("ok") is True]
    bad = [c["name"] for c in checks if c.get("ok") is False]
    na = [c["name"] for c in checks if c.get("ok") is None]
    summary = "\n".join([
        f"camctl Studio {APP_VERSION} camera report",
        f"Camera: {brand} {full['model']} (firmware {full['firmware'] or 'unknown'})",
        f"Connection: {connection}",
        "Works: " + (", ".join(ok) or "nothing yet"),
        "Does not work: " + (", ".join(bad) or "nothing"),
        "Not available on this camera: " + (", ".join(na) or "nothing"),
        "(Studio removed: serial number, names, network details. The full file is attached separately.)"])
    return full, summary


def file_stem(full: dict) -> str:
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in (s or "unknown"))
    return f"camera-report-{safe(full.get('brand'))}-{safe(full.get('model'))}-{safe(full.get('firmware'))}"


def save(folder: Path, full: dict, summary: str) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    stem = file_stem(full)
    j, t = folder / f"{stem}.json", folder / f"{stem}.txt"
    j.write_text(json.dumps(full, indent=1), encoding="utf-8")
    t.write_text(summary + "\n", encoding="utf-8")
    return j, t


def issue_link(full: dict, summary: str, base: str = ISSUE_URL) -> tuple[str | None, bool]:
    """(url to open, body too long -> paste it). None when no repository address is set."""
    if not base:
        return None, False
    title = f"Camera report: {full.get('brand')} {full.get('model')} {full.get('firmware')}".strip()
    url = base + "?" + urllib.parse.urlencode({"title": title, "body": summary, "labels": "camera-report"})
    if len(url) > MAX_URL:
        return base + "?" + urllib.parse.urlencode({"title": title, "labels": "camera-report"}), True
    return url, False
