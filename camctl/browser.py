"""Which browser Studio opens its window with, and which one the page checks drive headless.

Google Chrome first, then Microsoft Edge, then Chromium. CAMCTL_BROWSER=<path> picks
one by hand. Nothing is installed: if none is found, the launcher opens the default browser and the page checks
are skipped. Studio itself is a normal web page and works in any browser at http://127.0.0.1:8770.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _windows() -> list[Path]:
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    roots = [Path(r) for r in roots if r] or [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
    chrome = [r / "Google" / "Chrome" / "Application" / "chrome.exe" for r in roots]
    edge = [r / "Microsoft" / "Edge" / "Application" / "msedge.exe" for r in roots]
    return chrome + edge


def _other() -> list[Path]:
    out = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
           Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
           Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    for name in ("google-chrome", "google-chrome-stable", "microsoft-edge", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            out.append(Path(p))
    out += sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"))   # a test machine's Chromium
    return out


def candidates() -> list[Path]:
    own = os.environ.get("CAMCTL_BROWSER")
    return ([Path(own)] if own else []) + (_windows() if sys.platform == "win32" else _other())


def find() -> str | None:
    """Full path of the browser to use (Chrome, else Edge, else Chromium), or None."""
    return next((str(p) for p in candidates() if p.is_file()), None)


def name(path: str | None) -> str:
    p = Path(path).name.lower() if path else ""
    return "Edge" if "edge" in p else "Chromium" if "chromium" in p or (p == "chrome" and "pw-browsers" in str(path)) else "Chrome" if p else "none"


# extra flags for a headless run: a browser started as root on Linux (a test container) needs --no-sandbox
HEADLESS = ["--no-sandbox"] if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0 else []
