"""Writes docs/models.md: every camera model Studio knows, by maker and by what is expected to work.

    python tools/models_doc.py           # rewrite docs/models.md
    python tools/models_doc.py --check   # exit 1 if docs/models.md is out of date

The list comes only from the data files Studio itself uses (camctl/drivers/usb_models.json and cameras.json), so the
page and the program can never disagree. Reads files, writes one Markdown file, touches no camera.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVERS = ROOT / "camctl" / "drivers"
OUT = ROOT / "docs" / "models.md"

LIVE_OVER_USB = {"sony", "canon", "nikon"}          # Studio's own drivers that bring the live picture over the cable
ORDER = ("Sony", "Canon", "Nikon", "Fujifilm", "Panasonic", "OM System / Olympus", "Ricoh / Pentax", "Leica")
LEVELS = (
    ("full", "Full", "settings, photo, video and the live picture over one USB cable"),
    ("ctrl", "Settings and photo", "settings and shutter over USB; the live picture comes from the camera's own "
             "webcam mode (Studio picks it up by itself) or from an HDMI capture card"),
    ("rec", "Recognised only", "Studio names the camera and tells you what to change (for example the USB mode), "
            "but cannot control it in that mode or with that model"),
)
BRAND_NOTE = {
    "Sony": "The ZV-E10 is the only camera tested for real. Every other Sony uses the same driver, untested. A Sony in "
            "MTP / mass-storage mode is recognised but not controlled until USB is set to PC Remote. "
            "Recognised only below means older Sonys without PC Remote.",
    "Canon": "Only EOS bodies (DSLR, EOS M and EOS R) have the remote mode Studio uses; PowerShot and IXUS stay "
             "recognised only. The mode dial and the lens AF / MF switch stay on the camera.",
    "Nikon": "Bodies listed as settings and photo take pictures over USB but give no live picture over the cable.",
    "Fujifilm": "Standard PTP over USB (settings it lists, shutter). For the live picture use the camera's webcam "
                "mode (X Webcam) or an HDMI capture card.",
    "Panasonic": "Standard PTP over USB. Wi-Fi also brings the live picture; a photo taken over Wi-Fi stays on the card.",
    "OM System / Olympus": "Standard PTP over USB. Wi-Fi also brings the live picture.",
    "Ricoh / Pentax": "No model is controllable over USB in libgphoto2's list. Wi-Fi is the route for bodies with "
                      "the Wi-Fi API (Ricoh GR III / GR IIIx, Pentax K-1 II, KP, K-3 III and similar), untested.",
    "Leica": "Standard PTP over USB only; no Wi-Fi route and no live picture.",
}


def _clean(model: str) -> str:
    return model.split(" (")[0].strip()


def levels() -> dict[str, dict[str, list[str]]]:
    """brand -> {"full"|"ctrl"|"rec": sorted model names}, the same reading Studio makes of libgphoto2's list."""
    rows = json.loads((DRIVERS / "usb_models.json").read_text(encoding="utf-8"))["models"]
    brands = json.loads((DRIVERS / "cameras.json").read_text(encoding="utf-8"))["brands"]
    seen: dict[tuple[str, str], list[int]] = {}
    for brand, model, _vid, _pid, control, live in rows:
        n = _clean(model)
        if n and n.upper() != "PTP":
            a = seen.setdefault((brand, n), [0, 0])
            a[0] |= bool(control)
            a[1] |= bool(live)
    out: dict[str, dict[str, list[str]]] = {}
    for (brand, n), (control, live) in seen.items():
        if brand == "Canon" and not re.search(r"EOS|Rebel|Kiss", n):
            control = 0
        if brands.get(brand, {}).get("driver") not in LIVE_OVER_USB:
            live = 0
        lv = "full" if control and live else "ctrl" if control else "rec"
        out.setdefault(brand, {"full": [], "ctrl": [], "rec": []})[lv].append(n)
    for b in out.values():
        for k in b:
            b[k].sort(key=str.lower)
    return out


def _wifi(info: dict) -> str:
    if not (info.get("wifi") and info.get("wifi_driver")):
        return "no"
    if info.get("wifi_ip"):
        return f"yes, the camera's own network ({info['wifi_ip']})"
    return f"yes, over your Wi-Fi (port {info.get('wifi_port')})"


def render() -> str:
    data = json.loads((DRIVERS / "cameras.json").read_text(encoding="utf-8"))
    brands, tested = data["brands"], data["models"]
    lv = levels()
    total = sum(len(v) for b in lv.values() for v in b.values())
    rows = len(json.loads((DRIVERS / "usb_models.json").read_text(encoding="utf-8"))["models"])
    t = tested[0]
    L = [
        "# Camera models that should work",
        "",
        "<!-- Written by tools/models_doc.py from camctl/drivers/usb_models.json and cameras.json. "
        "Do not edit by hand: run python tools/models_doc.py -->",
        "",
        f"**Tested with a real camera: only the {t['brand']} {t['model']}** (firmware {t['firmware']}). Everything "
        "below is **untested**: written from public documentation (the PTP standard, libgphoto2's camera list, the "
        "makers' published Wi-Fi notes) and tried on simulated cameras only. \"Should work\" means Studio has a "
        "route for that camera, not that anyone has checked it.",
        "",
        f"Model names and USB ids come from libgphoto2's camera list ({rows} entries, {total} model names). Names are as libgphoto2 "
        "spells them, so one camera can appear twice under two spellings. Got one of these cameras? "
        "[Help add your camera](../README.md#help-add-your-camera): a camera check report from your camera moves it "
        "to tested.",
        "",
        "## Levels",
        "",
    ]
    L += [f"- **{name}**: {say}." for _, name, say in LEVELS]
    L += [
        "",
        "Not supported: GoPro and Blackmagic (they are controlled through their own apps). On Windows, a camera on the "
        "USB cable needs the WinUSB driver (for example with Zadig); Studio never installs a driver itself. Wi-Fi "
        "needs no driver.",
        "",
        "## Overview",
        "",
        "| Maker | Full | Settings and photo | Recognised only | Wi-Fi |",
        "|---|---|---|---|---|",
    ]
    for b in ORDER:
        v = lv.get(b, {"full": [], "ctrl": [], "rec": []})
        L.append(f"| [{b}](#{_anchor(b)}) | {len(v['full'])} | {len(v['ctrl'])} | {len(v['rec'])} | "
                 f"{_wifi(brands.get(b, {}))} |")
    L += ["", "Every Wi-Fi route except Sony's is untested. How each one works: "
              "[docs/cameras.md](cameras.md#routes).", ""]
    for b in ORDER:
        info, v = brands.get(b, {}), lv.get(b, {"full": [], "ctrl": [], "rec": []})
        L += [f"## {b}", "", BRAND_NOTE[b], ""]
        if info.get("wifi") and info.get("wifi_driver"):
            how = " ".join(info.get("wifi_setup") or ["MENU > Network > PC Remote Function: On; Studio finds it on "
                                                       "your Wi-Fi."])
            L += [f"**Wi-Fi** ({'tested on the ZV-E10' if b == 'Sony' else 'untested'}): {how}", ""]
        for k, name, _ in LEVELS:
            if v[k]:
                L += [f"**{name}** ({len(v[k])}): " + ", ".join(v[k]), ""]
    return "\n".join(L)


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only check that docs/models.md is up to date")
    a = ap.parse_args()
    text = render()
    if a.check:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("docs/models.md is up to date" if ok else "docs/models.md is out of date: run python tools/models_doc.py")
        return 0 if ok else 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
