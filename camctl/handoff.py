"""Hand-off to post: shoot folders, DaVinci Resolve, Lightroom / Photoshop.

Studio shoots for the grade (S-Log3 / RAW) and hands over clean material. Nothing is
ever moved or deleted: files are COPIED into a shoot folder; looks are exported as a
starting point only (.cube LUT for Resolve, .xmp preset for Lightroom / Camera Raw).

Shoot folder layout (in Pictures\\camctl Shoots\\<date> <name>):
  VIDEO\\  PHOTO RAW\\  PHOTO JPEG\\  LUTS\\  LIGHTROOM\\  LIGHTING\\  NOTES\\
"""
from __future__ import annotations

import colorsys
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import lut

SHOOTS = Path(os.environ.get("USERPROFILE") or Path.home()) / "Pictures" / "camctl Shoots"
SUBDIRS = ("VIDEO", "PHOTO RAW", "PHOTO JPEG", "LUTS", "LIGHTROOM", "LIGHTING", "NOTES")
STATE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl" / "shoot.json"
PHOTOSHOP = Path(r"C:\Program Files\Adobe\Adobe Photoshop 2027\Photoshop.exe")
RESOLVE = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe")


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).strip()[:60] or "Shoot"


# ---------------------------------------------------------------- shoot folders
def current() -> dict | None:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if Path(d["path"]).is_dir() else None
    except (OSError, ValueError, KeyError):
        return None


def new_shoot(name: str) -> dict:
    folder = SHOOTS / f"{time.strftime('%Y-%m-%d')} {_safe(name)}"
    n = 2
    while folder.exists():
        folder = SHOOTS / f"{time.strftime('%Y-%m-%d')} {_safe(name)} ({n})"
        n += 1
    for s in SUBDIRS:
        (folder / s).mkdir(parents=True, exist_ok=True)
    d = {"name": _safe(name), "path": str(folder), "created": time.time()}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d), encoding="utf-8")
    return d


def ensure_shoot() -> dict:
    return current() or new_shoot("Shoot")


def add_file(src: Path, kind: str | None = None) -> Path:
    """Copy a photo/clip into the current shoot (never moves or deletes)."""
    sh = Path(ensure_shoot()["path"])
    ext = src.suffix.lower()
    sub = kind or ("VIDEO" if ext in (".mp4", ".mov", ".mxf") else "PHOTO RAW" if ext in (".arw", ".dng") else "PHOTO JPEG")
    dst = sh / sub / src.name
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    return dst


def summary(sh: Path) -> dict:
    count = lambda s: len([f for f in (sh / s).iterdir() if f.is_file()]) if (sh / s).is_dir() else 0
    return {s: count(s) for s in SUBDIRS}


def write_notes(sh: Path, settings: list[dict], notes: str = "", plan: dict | None = None, svg: str = "") -> None:
    lines = [f"# {sh.name}", "", time.strftime("Saved %Y-%m-%d %H:%M"), ""]
    if notes:
        lines += ["## Notes", notes, ""]
    lines += ["## Camera settings"] + [f"- {s.get('friendly') or s['name']}: {s['label']}" for s in settings] + [""]
    lines += ["## Colour (for the grade)",
              "- Video: Sony S-Gamut3.Cine / S-Log3 (PP8). In Resolve: input colour space 'S-Gamut3.Cine/S-Log3'.",
              "- Photos: RAW (.ARW) in PHOTO RAW; JPEGs are only a reference.", ""]
    if plan:
        lines += [f"## Lighting: {plan.get('title', '')}", f"Key:fill {plan.get('key_fill_ratio', '')}, {plan.get('contrast', '')}", ""]
        lines += [f"{i + 1}. {L['role'].upper()}: {L['fixture']} ({L['modifier']}), {round(L['angle_deg'])} deg, "
                  f"{L['distance_m']:.1f} m, h {L['height_m']:.1f} m, {L.get('color_temp_k') or ''} K"
                  for i, L in enumerate(plan.get("lights", []))]
        lines += [""] + [f"- {s}" for s in plan.get("steps", [])]
        (sh / "LIGHTING" / "lighting_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    if svg:
        (sh / "LIGHTING" / "lighting_diagram.svg").write_text(svg, encoding="utf-8")
    (sh / "NOTES" / "shoot_notes.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- looks -> LUT / XMP (starting points)
def export_luts(sh: Path, look_key: str | None, look: dict | None) -> list[str]:
    out = []
    d = sh / "LUTS"
    tech = d / "camctl S-Log3.Cine to Rec709.cube"
    tech.write_text(lut.export_cube(None, "camctl S-Log3.Cine to Rec709", size=33, source="slog3cine"))
    out.append(tech.name)
    if look:
        name = look.get("name", look_key or "look")
        for src, suffix in (("slog3cine", "from S-Log3"), ("rec709", "for Rec709")):
            f = d / f"camctl {name} ({suffix}).cube"
            f.write_text(lut.export_cube(look["grade"], f"camctl {name}", size=33, source=src))
            out.append(f.name)
    return out


def _hue_sat(rgb: list[float], amount: float) -> tuple[int, int]:
    r, g, b = (0.5 + 1.5 * c for c in rgb)
    h, s, _v = colorsys.rgb_to_hsv(*(min(max(c, 0), 1) for c in (r, g, b)))
    mag = math.sqrt(sum(c * c for c in rgb))
    return int(round(h * 360)) % 360, int(min(100, round(mag * amount * 160)))


def xmp_preset(name: str, g: dict) -> str:
    """A Lightroom / Camera Raw preset approximating the Studio look (editable starting point)."""
    s = {
        "Exposure2012": f"{math.log2(g.get('gain', 1.0)):+.2f}",
        "Contrast2012": str(int(round((g.get("contrast", 1.0) - 1) * 100))),
        "Blacks2012": str(int(round(g.get("lift", 0) * 250 - g.get("crush", 0) * 400))),
        "Saturation": str(int(round((g.get("sat", 1.0) - 1) * 100))) if not g.get("mono") else "0",
        "ConvertToGrayscale": "True" if g.get("mono") else "False",
        "GrainAmount": str(int(round(g.get("grain", 0) * 40))),
    }
    t = g.get("temp", 0)
    if t:
        s.update(ColorGradeGlobalHue="45" if t > 0 else "215", ColorGradeGlobalSat=str(int(min(30, abs(t) * 40))))
    if g.get("split"):
        sh, ss = _hue_sat(g.get("shadows", [0, 0, 0]), g["split"])
        hh, hs = _hue_sat(g.get("highlights", [0, 0, 0]), g["split"])
        s.update(ColorGradeShadowHue=str(sh), ColorGradeShadowSat=str(ss),
                 ColorGradeHighlightHue=str(hh), ColorGradeHighlightSat=str(hs), ColorGradeBlending="50")
    attrs = "\n".join(f'   crs:{k}="{v}"' for k, v in s.items())
    uid = re.sub(r"[^A-F0-9]", "", (name.encode().hex().upper() + "0" * 32))[:32]
    return f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="camctl Studio">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="Normal"
   crs:Cluster="camctl Studio"
   crs:UUID="{uid}"
   crs:SupportsAmount2="True"
   crs:SupportsColor="True"
   crs:SupportsMonochrome="True"
   crs:SupportsHighDynamicRange="True"
   crs:SupportsNormalDynamicRange="True"
   crs:SupportsSceneReferred="True"
   crs:SupportsOutputReferred="True"
   crs:Version="15.0"
   crs:ProcessVersion="11.0"
{attrs}
   crs:HasSettings="True">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">camctl {name}</rdf:li></rdf:Alt></crs:Name>
   <crs:Group><rdf:Alt><rdf:li xml:lang="x-default">camctl Studio</rdf:li></rdf:Alt></crs:Group>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
'''


def export_xmp(sh: Path, look: dict) -> str:
    f = sh / "LIGHTROOM" / f"camctl {look['name']}.xmp"
    f.write_text(xmp_preset(look["name"], look["grade"]), encoding="utf-8")
    return f.name


# ---------------------------------------------------------------- Resolve
RESOLVE_SCRIPT = r'''# camctl Studio -> DaVinci Resolve: create a project for this shoot with S-Log3 colour settings.
# Run inside Resolve: Workspace > Scripts (copy this file to
# %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\ first), or from Resolve Studio's console.
import os
SHOOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = os.path.basename(SHOOT)
try:
    resolve = app.GetResolve()            # inside Resolve
except NameError:
    import DaVinciResolveScript as dvr    # external scripting (Resolve Studio)
    resolve = dvr.scriptapp("Resolve")
pm = resolve.GetProjectManager()
proj = pm.CreateProject(NAME) or pm.LoadProject(NAME)
proj.SetSetting("colorScienceMode", "davinciYRGBColorManagedv2")
proj.SetSetting("colorSpaceInput", "S-Gamut3.Cine/S-Log3")
proj.SetSetting("colorSpaceTimeline", "DaVinci WG/Intermediate")
proj.SetSetting("colorSpaceOutput", "Rec.709 Gamma 2.4")
mp = proj.GetMediaPool()
root = mp.GetRootFolder()
for sub, bin_name in (("VIDEO", "Video (S-Log3)"), ("PHOTO RAW", "Stills")):
    folder = os.path.join(SHOOT, sub)
    files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))] if os.path.isdir(folder) else []
    if files:
        b = mp.AddSubFolder(root, bin_name)
        mp.SetCurrentFolder(b)
        mp.ImportMedia(files)
pm.SaveProject()
print("camctl: project", NAME, "ready (S-Gamut3.Cine/S-Log3 input). LUTs are in", os.path.join(SHOOT, "LUTS"))
'''

RESOLVE_README = """Send to DaVinci Resolve
=======================
Clips: VIDEO\\   (Sony S-Log3 / S-Gamut3.Cine, PP8)
LUTs:  LUTS\\    (technical S-Log3 -> Rec.709, and the Studio look as a starting point)

Easiest:
1. Open Resolve, create a project, Project Settings > Color Management:
   Color science "DaVinci YRGB Color Managed", input "S-Gamut3.Cine/S-Log3",
   timeline "DaVinci WG/Intermediate", output "Rec.709 Gamma 2.4".
2. Drag the VIDEO folder into the Media Pool.
Or run NOTES\\resolve_import.py from Workspace > Scripts (see the first lines of the script).
"""


def send_to_resolve(sh: Path) -> dict:
    (sh / "NOTES" / "resolve_import.py").write_text(RESOLVE_SCRIPT, encoding="utf-8")
    (sh / "NOTES" / "SEND TO RESOLVE.txt").write_text(RESOLVE_README, encoding="utf-8")
    return {"installed": RESOLVE.exists(), "folder": str(sh),
            "steps": ["Open DaVinci Resolve and create a project.",
                      "Project Settings > Color Management: DaVinci YRGB Color Managed, input S-Gamut3.Cine/S-Log3.",
                      f"Drag “{sh / 'VIDEO'}” into the Media Pool."] if RESOLVE.exists() else
                     ["DaVinci Resolve isn't installed on this PC yet (free at blackmagicdesign.com).",
                      f"The shoot folder is ready: {sh}", "Open “NOTES\\SEND TO RESOLVE.txt” there for the 2 steps."]}


# ---------------------------------------------------------------- Photoshop / Explorer
def open_in_photoshop(path: Path) -> None:
    if not PHOTOSHOP.exists():
        raise FileNotFoundError("Photoshop isn't installed at the usual place.")
    subprocess.Popen([str(PHOTOSHOP), str(path)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def show_folder(path: Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]  # opens Explorer
