"""Start the Studio server with no console window of its own (like the Studio launcher does), so a console being
closed or a Ctrl event elsewhere can never stop it. Output goes to %APPDATA%\\camctl\\logs\\webui.out."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
py = Path(sys.executable).with_name("python.exe")
log = Path(os.environ.get("APPDATA") or Path.home()) / "camctl" / "logs" / "webui.out"
log.parent.mkdir(parents=True, exist_ok=True)
subprocess.Popen([str(py), "-m", "camctl.webui", "--no-browser"], cwd=str(ROOT),
                 creationflags=0x08000000 | 0x00000200,          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                 stdout=open(log, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
