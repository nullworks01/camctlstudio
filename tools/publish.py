"""What may be published (GitHub), decided in one place, and the guard that checks it.

    python tools/publish.py --list            # writes publish_list.txt (path and size of every file that would go out)
    python tools/publish.py --check           # the guard: fails if a published file holds anything personal
    python tools/publish.py --copy <folder>   # a clean copy of exactly those files (never the project folder itself)

Nothing here uploads anything. Personal and machine-specific things live outside the code folder (%APPDATA%\\camctl:
key file, wifi.json pairing, PIN, logs, setups; photos in Pictures) or are never selected (captures, logs, backups,
screenshots with real camera frames, notes).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# allow-list: only these are ever published
INCLUDE = [
    "camctl/**/*.py", "camctl/studio_pro.html", "camctl/presets.json", "camctl/assets/*.ico", "camctl/assets/looks_sample.jpg", "camctl/assets/cards/*.jpg",
    "tests/*.py", "tests/data/*.json", "tests/data/lineup_ref.jpg",
    "tools/battery_guard.py", "tools/check_page.py", "tools/leave_as_set_up.py",
    "tools/publish.py", "tools/sim_studio.py", "tools/start_server.pyw", "tools/studio_app.pyw",
    "README.md", "LICENSE", "CONTRIBUTING.md", "requirements.txt", "requirements-optional.txt", "pyproject.toml", "start_studio.ps1",
    "Install or repair camctl Studio.cmd", ".gitignore", ".github/ISSUE_TEMPLATE/*.md",
    "docs/guide.md", "docs/photo-credits.md", "docs/img/*.jpg",
]
NEVER = re.compile(r"(\.bak$|\.r\d+\.bak$|__pycache__|\.pyc$|/logs/|/captures/|AUDIT\.md|publish_list\.txt|\.log$)", re.I)
IMAGES_OK = {"tests/data/lineup_ref.jpg", "camctl/assets/studio.ico",     # a generated picture and the app icon
             "camctl/assets/looks_sample.jpg"}   # the Looks sample: stock photo (Vitaly Gariev, Unsplash License)
# the pictures on the shot cards and special-shot cards: stock photos (Unsplash License), named one by one
CARD_PICS = ("portrait", "interview", "product", "daylight", "lowlight", "focusstack", "burst", "match", "auto")
IMAGES_OK |= {f"camctl/assets/cards/{n}.jpg" for n in CARD_PICS}
# the visual guide's pictures (docs/img): screenshots of the SIMULATOR only, never a camera frame. The person in them is
# a free stock photo (Vitaly Gariev on Unsplash, Unsplash License) put in the place of the simulator's test card.
# Each picture is named here so that no other picture can slip in
GUIDE_PICS = (
    "claude", "claude-answer", "cover", "focus-stack", "getting-ready", "ipad", "iso", "looks", "main", "main-numbered",
    "more", "photo-review", "photo-viewer", "playground", "recording", "settings-camera", "settings-format",
    "settings-light", "settings-setups", "shoot-handoff", "shot", "sound-check", "special-shots", "teleprompter",
    "video", "white-balance",
)
IMAGES_OK |= {f"docs/img/{n}.jpg" for n in GUIDE_PICS}
# written WITHOUT a UTF-8 byte-order mark: GitHub reads the issue template's header only without one
NO_BOM = re.compile(r"^(\.gitignore|\.github/.*)$")
BOM = b"\xef\xbb\xbf"

# the guard: nothing personal, nothing secret, nothing from his network or camera
PATTERNS = {
    "user name": re.compile("fh" + "ars", re.I),          # built from parts so this file does not match itself
    "first name": re.compile(r"\b" + "Fr" + r"anz\b"),
    "family name / email": re.compile("har" + "ster", re.I),
    "user path": re.compile(r"[A-Z]:[\\/]+Users[\\/]+(?!<|you|USER|\{)", re.I),
    "home network address": re.compile(r"\b192\.168\.(?!122\.1\b)\d{1,3}\.\d{1,3}\b"),   # 192.168.122.1 = Sony's own AP
    "MAC address": re.compile(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", re.I),
    "Claude key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),          # any key-shaped string, also invented test keys
    "tunnel / connector URL": re.compile(r"https://[a-z0-9-]+\.(trycloudflare\.com|ngrok\.io)", re.I),
    "pairing GUID / token": re.compile(r"%21[0-9a-f]{32}%21|\b[0-9a-f]{32}\b(?=.*liveview)", re.I),
}


def files() -> list[Path]:
    out = set()
    for pat in INCLUDE:
        for p in ROOT.glob(pat):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and not NEVER.search("/" + rel):
                out.add(p)
    return sorted(out, key=lambda p: p.relative_to(ROOT).as_posix())


def check() -> list[str]:
    problems = []
    for p in files():
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".arw", ".tif", ".heif", ".mp4", ".mov", ".ico") and rel not in IMAGES_OK:
            problems.append(f"{rel}: picture or video file")
            continue
        if p.suffix.lower() in (".jpg", ".ico"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for name, rx in PATTERNS.items():
            m = rx.search(text)
            if m:
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{rel}:{line}: {name}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--copy")
    a = ap.parse_args()
    fs = files()
    if a.list:
        lines = [f"{p.relative_to(ROOT).as_posix()}\t{p.stat().st_size}" for p in fs]
        (ROOT / "publish_list.txt").write_text("\n".join(lines) + f"\n\n{len(fs)} files, "
                                               f"{sum(p.stat().st_size for p in fs) / 1e6:.2f} MB\n", encoding="utf-8")
        print(f"{len(fs)} files listed in publish_list.txt")
    if a.check or a.copy:
        probs = check()
        for x in probs:
            print("FAIL " + x)
        print("Guard: nothing personal found." if not probs else f"Guard: {len(probs)} problem(s).")
        if probs:
            return 1
    if a.copy:
        dest = Path(a.copy).resolve()
        if dest == ROOT or ROOT in dest.parents:
            print("Refused: the copy must be outside the project folder.")
            return 1
        if dest.exists() and any(dest.iterdir()):
            shutil.rmtree(dest)
        for p in fs:
            t = dest / p.relative_to(ROOT)
            t.parent.mkdir(parents=True, exist_ok=True)
            if NO_BOM.match(p.relative_to(ROOT).as_posix()):
                data = p.read_bytes()
                t.write_bytes(data[len(BOM):] if data.startswith(BOM) else data)
                shutil.copystat(p, t)
            else:
                shutil.copy2(p, t)
        print(f"Copied {len(fs)} files to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
