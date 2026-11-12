"""What Studio needs, read from the one list (requirements.txt), and whether it is there.

    python -m camctl.needs      # prints in plain words what is there and what is missing; exit code 1 if a required one is missing

Studio's start check uses the same function, so the list is the single source of truth.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REQ = Path(__file__).resolve().parents[1] / "requirements.txt"
_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)==([^\s#]+)\s*#\s*module=(\S+)\s+(required|optional)\s+(.*)$")


def entries() -> list[dict]:
    out = []
    for line in REQ.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m:
            out.append({"package": m[1], "version": m[2], "module": m[3], "required": m[4] == "required", "what": m[5].strip()})
    return out


def _present(e: dict) -> bool:
    if e["module"] == "imageio_ffmpeg":            # the job is "a working ffmpeg": an installed ffmpeg counts too
        try:
            from . import video
            return bool(video.find_ffmpeg())
        except Exception:  # noqa: BLE001
            return False
    try:
        return importlib.util.find_spec(e["module"]) is not None
    except (ImportError, ValueError):
        return False


def check() -> dict:
    """{'missing_required': [...], 'missing_optional': [...]} as entries."""
    miss = [e for e in entries() if not _present(e)]
    return {"missing_required": [e for e in miss if e["required"]], "missing_optional": [e for e in miss if not e["required"]]}


def sentence(e: dict) -> str:
    name = "ffmpeg (video tool)" if e["module"] == "imageio_ffmpeg" else e["package"]
    return f"{name} is missing: {e['what']} won't work until it is installed (run “Install or repair camctl Studio”)."


def main() -> int:
    c = check()
    if not c["missing_required"] and not c["missing_optional"]:
        print("Everything Studio needs is installed.")
        return 0
    for e in c["missing_required"]:
        print("MISSING (needed): " + sentence(e))
    for e in c["missing_optional"]:
        print("Missing (optional): " + sentence(e))
    if c["missing_required"]:
        print("Studio is NOT ready: a needed library is missing.")
        return 1
    print("Studio is ready. Some optional parts are missing (see above).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
