"""camctl Studio as a standalone window (no console, no browser tabs or address bar).

Started from the "camctl Studio" desktop / Start menu shortcut with pythonw.exe:
  1. if Studio's server is not answering on 127.0.0.1:8770, start it hidden (no console window);
  2. if the Studio window is already open, bring it to the front instead of opening a second one;
  3. open Studio in its own window (Microsoft Edge in app mode, with its own profile in %APPDATA%\\camctl\\app-window
     so it remembers size and position and never mixes with normal browsing);
  4. when the window is closed: release the camera cleanly (closes the PTP session, so the camera does not refuse
     the next connection) and stop the server this launcher started.
Nothing is installed; no .exe is built (Smart App Control).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8770/"
BASE = Path(os.environ.get("APPDATA") or Path.home()) / "camctl"
PROFILE = BASE / "app-window"
LOG = BASE / "logs" / "app.log"
TITLE = "camctl Studio"
NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def server_up() -> bool:
    try:
        urllib.request.urlopen(URL + "api/health", timeout=1.5).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def edge() -> str | None:
    for p in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
        c = Path(p) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if p and c.exists():
            return str(c)
    return None


def window_open() -> bool:
    """Is our Studio window already open? Then bring it to the front (single instance)."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == TITLE:              # the app window only (a browser tab adds " - Edge/Chrome")
                found.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    if found:
        h = found[0]
        user32.ShowWindow(h, 9)            # restore if minimised
        user32.SetForegroundWindow(h)
        return True
    return False


def start_server():
    py = Path(sys.executable).with_name("python.exe")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    out = open(BASE / "logs" / "webui.out", "a", encoding="utf-8")
    p = subprocess.Popen([str(py if py.exists() else sys.executable), "-m", "camctl.webui", "--no-browser"],
                         cwd=str(ROOT), creationflags=NO_WINDOW, stdout=out, stderr=subprocess.STDOUT)
    log(f"started the Studio server (pid {p.pid})")
    for _ in range(60):
        if server_up():
            break
        time.sleep(0.5)
    return p


def stop_server(started) -> None:
    """Release the camera and stop the server, but only the one THIS launcher started and only if it still runs."""
    if started is None or started.poll() is not None:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(URL + "api/shutdown", method="POST"), timeout=10).read()
        log("camera released and server stopped")
    except Exception as exc:  # noqa: BLE001
        log(f"shutdown request failed: {exc}")
    try:
        started.wait(timeout=10)
    except subprocess.TimeoutExpired:
        started.kill()
        log("server did not stop in time: ended it")


def wait_window_closed() -> None:
    for _ in range(24 * 3600):
        if not window_open_quiet():
            return
        time.sleep(1)


def main() -> None:
    sys.path.insert(0, str(ROOT))
    try:                                                     # a home-network (iPad) Studio becomes laptop-only again
        from camctl import launch
        mode = launch.prepare("local")
        if mode == 0 and server_up():
            log("could not stop the running Studio")
    except Exception as exc:  # noqa: BLE001
        log(f"mode check failed: {exc}")
    started = None if server_up() else start_server()       # the server first, so the window always has one
    if window_open():
        log("already open: focused the existing window")
        if started is not None:                              # we (re)started the server for an open window
            wait_window_closed()
            log("window closed")
            stop_server(started)
        return
    ed = edge()
    if not ed:
        import webbrowser
        webbrowser.open(URL)
        log("Edge not found: opened the default browser instead")
        return
    PROFILE.mkdir(parents=True, exist_ok=True)
    win = subprocess.Popen([ed, f"--app={URL}", f"--user-data-dir={PROFILE}", "--no-first-run",
                            "--no-default-browser-check", "--disable-features=Translate", "--start-maximized"])
    log(f"opened the Studio window (pid {win.pid})")
    # Edge often hands the window to an already running Edge process and this one exits at once, and the
    # window's title only becomes "camctl Studio" once the page has loaded: first wait until the window is
    # really there, then until it is really gone (never stop the server 5 s after opening it).
    for _ in range(60):
        if window_open_quiet():
            break
        time.sleep(0.5)
    win.wait()
    wait_window_closed()
    log("window closed")
    stop_server(started)


def window_open_quiet() -> bool:
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == TITLE:              # the app window only (a browser tab adds " - Edge/Chrome")
                found.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    return bool(found)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"launcher error: {exc}")
