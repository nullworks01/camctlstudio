"""MCP server that gives Claude control of cameras.

Run:  python -m camctl.server            (stdio, for Claude Desktop / Claude Code)
      python -m camctl.server --http     (HTTP on 127.0.0.1:8765/mcp)
"""
from __future__ import annotations

import argparse
import io
import threading
import time
from pathlib import Path

import functools

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .drivers.sony import DEFAULT_HOST, FMT_EXT, PROPS, SonyCamera, format_value
from .ptpip import PTPError

mcp = MCPServer(
    name="camctl",
    version="0.1.0",
    instructions=(
        "Controls physical cameras. Call camera_connect first, then camera_live_view to "
        "see through the camera before changing settings or shooting. Use "
        "camera_list_settings to learn valid values: set values exactly as listed in "
        "'choices' (raw numbers, e.g. aperture 280 = f/2.8, iso 16777215 = auto)."
    ),
)

_cam: SonyCamera | None = None
_lock = threading.Lock()


def _camera() -> SonyCamera:
    if _cam is None:
        raise RuntimeError("No camera connected. Call camera_connect first.")
    return _cam


def _tool():
    """mcp.tool() that reports camera problems to Claude in plain words.

    The SDK hides the text of unexpected exceptions from the client, so camera
    and USB errors (PTPError, OSError incl. usb.core.USBError, RuntimeError,
    ValueError) are re-raised as ToolError, whose message reaches Claude.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except (PTPError, OSError, RuntimeError, ValueError) as exc:
                raise ToolError(str(exc) or type(exc).__name__) from exc
        return mcp.tool()(wrapper)
    return deco


@_tool()
def camera_connect(host: str = "usb") -> str:
    """Connect to a Sony camera.

    host: "usb" for a USB cable (camera USB Connection set to 'PC Remote'), or the
          camera's IP for Wi-Fi ('Ctrl w/ Smartphone' mode, usually 192.168.122.1).
    """
    global _cam
    with _lock:
        if _cam is not None:
            _cam.close()
        cam = SonyCamera(host)
        cam.connect()
        _cam = cam
        return f"Connected to {cam.name} at {host}."


@_tool()
def camera_disconnect() -> str:
    """Disconnect from the camera."""
    global _cam
    with _lock:
        if _cam is not None:
            _cam.close()
            _cam = None
    return "Disconnected."


@_tool()
def camera_live_view() -> Image:
    """Return the current live-view frame so you can see what the camera sees."""
    with _lock:
        return Image(data=_camera().live_view(), format="jpeg")


@_tool()
def camera_status() -> dict:
    """Key exposure settings in human-readable form."""
    with _lock:
        props = _camera().refresh()
    return {name: format_value(code, props[code].value)
            for name, code in PROPS.items() if code in props}


@_tool()
def camera_list_settings() -> list[dict]:
    """Every setting the camera exposes, with current value, whether it can be
    changed right now, and the allowed choices or range."""
    with _lock:
        return _camera().settings()


@_tool()
def camera_set_setting(setting: str, value: int) -> str:
    """Change a setting.

    setting: a friendly name (iso, aperture, shutter_speed, exposure_compensation,
             white_balance, focus_mode, exposure_mode, drive_mode) or a hex code like '0xD21E'.
    value:   raw value exactly as shown in camera_list_settings choices.
    """
    with _lock:
        cam = _camera()
        cam.set(setting, value)
        code = cam.resolve(setting)
        new = cam.refresh().get(code)
    return f"{setting} is now {format_value(code, new.value) if new else value}."


@_tool()
def camera_autofocus() -> str:
    """Half-press the shutter to autofocus."""
    with _lock:
        _camera().autofocus()
    return "Autofocus triggered."


CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"


def _save_captures(files: list[tuple[int, bytes]]) -> tuple[list[Path], bytes | None]:
    CAPTURE_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths, jpeg = [], None
    for i, (fmt, data) in enumerate(files):
        ext = FMT_EXT.get(fmt) or (".jpg" if data[:2] == b"\xff\xd8" else ".bin")
        path = CAPTURE_DIR / f"{stamp}_{i + 1}{ext}"
        path.write_bytes(data)
        paths.append(path)
        if ext == ".jpg":
            jpeg = data
    return paths, jpeg


def _preview(jpeg: bytes, max_side: int = 1600) -> bytes:
    """Downscale for returning to Claude; the full file stays on disk."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return jpeg
    im = PILImage.open(io.BytesIO(jpeg))
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


@_tool()
def camera_take_photo():
    """Autofocus and take a photo. Saves every transferred file (JPEG and RAW)
    in the project's 'captures' folder and returns the JPEG plus the file paths."""
    with _lock:
        files = _camera().capture()
    if not files:
        return ("Shutter released, but the camera transferred no file. The photo is on "
                "the memory card only (or nowhere if the camera saves to PC only).")
    paths, jpeg = _save_captures(files)
    text = "Saved: " + ", ".join(str(p) for p in paths)
    if jpeg:
        return [text, Image(data=_preview(jpeg), format="jpeg")]
    return text + " (no JPEG was transferred, only RAW)."


@_tool()
def camera_toggle_recording() -> str:
    """Start video recording, or stop it if already recording."""
    with _lock:
        _camera().movie_record(True)
    return "Movie record button pressed."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--path", default=None,
                    help="HTTP endpoint path (default /mcp). For remote access use an "
                         "unguessable secret path; env CAMCTL_HTTP_PATH also works.")
    args = ap.parse_args()
    if args.http:
        import os
        path = args.path or os.environ.get("CAMCTL_HTTP_PATH") or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        from mcp.server.transport_security import TransportSecuritySettings
        # Host check stays on: only localhost Host headers are accepted, so a
        # tunnel must rewrite Host to 127.0.0.1 (cloudflared: --http-host-header).
        # Origins: local tools plus Claude's web apps (the remote connector).
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                             "https://claude.ai", "https://claude.com"],
        )
        # Plain JSON replies and no server-side sessions: works through tunnels
        # that buffer streaming (SSE) responses, and with clients that retry from
        # different addresses. Any path other than `path` is 404.
        mcp.run("streamable-http", host=args.host, port=args.port, streamable_http_path=path,
                json_response=True, stateless_http=True, transport_security=security)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
