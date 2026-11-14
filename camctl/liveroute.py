"""Where the live picture comes from, chosen per camera: the camera's own USB live view, the camera's webcam mode, or
an HDMI capture card. UNTESTED WITH REAL CAMERAS OTHER THAN THE ZV-E10 (whose picture still comes from its own USB
live view, exactly as before).

Order, best first:
  1. the camera driver's live view over PTP (Sony, Canon EOS, Nikon; the fastest and in step with every setting),
  2. the camera's own webcam mode (Fujifilm "X Webcam", Panasonic "LUMIX Webcam", OM System, Canon / Nikon / Sony
     webcam apps, Sony USB streaming): a video device named after the maker. It is the connected camera itself, so
     Studio uses it by itself when the camera gives no PTP live view; it is never a laptop webcam,
  3. an HDMI capture card (Cam Link and similar): works with every camera that has a clean HDMI output. Studio
     never opens one by itself; it offers it, and he picks it (More › Capture card), as before.
When none is there, Studio says in words which of these would give this camera a picture.
"""
from __future__ import annotations

import re

# names makers' webcam modes and webcam apps report (matched case-insensitively against the device name)
WEBCAM_NAMES = {
    "Fujifilm": r"fujifilm|x webcam|\bx-(h|s|t|e|pro)\d|\bgfx",
    "Panasonic": r"lumix",
    "OM System / Olympus": r"om webcam|om-d|om system|olympus",
    "Canon": r"eos webcam|canon",
    "Nikon": r"nikon",
    "Sony": r"sony|imaging edge webcam|\bzv-|\bilce|\bilme|\bfx\d",
    "Ricoh / Pentax": r"ricoh|pentax",
    "Leica": r"leica",
}
# names capture cards report
CARD_NAMES = r"cam ?link|hdmi|capture|usb video|elgato|avermedia|magewell|ezcap|hagibis|guermok|ugreen|razer ripsaw|live gamer"

# per maker: how its webcam mode is switched on (shown when no picture is found)
WEBCAM_HOWTO = {
    "Fujifilm": "Fujifilm: Connection Setting › PC Connection Mode › USB Webcam (newer bodies; older ones use the "
                "FUJIFILM X Webcam app).",
    "Panasonic": "Panasonic: USB Mode › PC (Webcam) on newer LUMIX bodies (older ones use the LUMIX Webcam app).",
    "OM System / Olympus": "OM System: on connecting choose Webcam (or the OM Webcam app).",
    "Canon": "Canon: the EOS Webcam Utility app.",
    "Nikon": "Nikon: the Nikon Webcam Utility app, or USB streaming (UVC) on newer Z bodies.",
    "Sony": "Sony: MENU › Network › USB Streaming on bodies that have it (or Imaging Edge Webcam).",
}


def webcam_for(brand: str | None, devices: list[str]) -> str | None:
    """The video device that is this maker's webcam mode, if one is there."""
    pat = WEBCAM_NAMES.get(brand or "")
    if not pat:
        return None
    for name in devices:
        if re.search(pat, name, re.I) and not re.search(CARD_NAMES, name, re.I):
            return name
    return None


def cards_in(devices: list[str]) -> list[str]:
    return [n for n in devices if re.search(CARD_NAMES, n, re.I)]


def choose(brand: str | None, model: str | None, ptp_live: bool, devices: list[str]) -> dict:
    """The best picture route for this camera. kind: ptp / webcam / card_offer / none. `auto` says whether Studio may
    switch to it by itself (only the camera's own picture: PTP or its webcam mode)."""
    name = f"{brand} {model}".strip() if brand else (model or "This camera")
    if ptp_live:
        return {"kind": "ptp", "auto": True, "say": ""}
    cam = webcam_for(brand, devices)
    if cam:
        return {"kind": "webcam", "auto": True, "device": cam,
                "say": f"Picture from the {name}'s webcam mode over USB (untested route)."}
    cards = cards_in(devices)
    if cards:
        return {"kind": "card_offer", "auto": False, "devices": cards,
                "say": f"The {name} gives no live picture over its control cable. A capture card is connected: "
                       "choose it in More › Capture card to see the picture."}
    tips = ["an HDMI capture card on the camera's HDMI output (More › Capture card)"]
    if brand in WEBCAM_HOWTO:
        tips.append("its webcam mode: " + WEBCAM_HOWTO[brand].split(": ", 1)[1].rstrip("."))
    return {"kind": "none", "auto": False,
            "say": f"The {name} gives no live picture over its control cable. For a picture use " + ", or ".join(tips)
            + ". Settings and the shutter work."}
