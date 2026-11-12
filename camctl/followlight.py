"""'Follow the light': keep video exposure right while the light changes.

Works for S-Log3 (keeps the picture a chosen number of stops over mid-grey, the
usual +1 to +2 for clean log) and for normal video. It watches the live-view
frames that Studio already receives, so it adds no extra traffic per frame.

Rules, so the footage doesn't pump:
- the 180-degree shutter (1/50) is never touched;
- nothing changes while the error is inside a dead band (±0.4 stop);
- a change only happens after the error has lasted ~1.5 s (no reaction to a
  passing shadow), then the camera settles before the next decision;
- while recording it moves one small step (1/3 stop) at a time;
- ISO moves first, within the camera's range and the noise-guard maximum,
  then the aperture (unless the iris is locked for constant depth of field).
Nothing records or shoots on its own.
"""
from __future__ import annotations

import math
import threading
import time

from . import presets
from .drivers.sony import PROPS

LOG_PROFILES = (8, 9)          # PP8 S-Log3.Cine, PP9 S-Log3
MOVIE_M = presets.MODE_MOVIE_M
DEAD_BAND = 0.4                 # stops
HOLD_S = 1.5                    # error must last this long before acting
SETTLE_S = 1.2                  # wait after a change before judging again
TICK_S = 0.25


def error_stops(level: float, log: bool, over: float) -> float:
    """How many stops the picture must get brighter (+) or darker (-)."""
    if log:
        return (presets.log_target(over) - level) / presets.SLOG3_STOP
    return math.log2(presets._lin(118) / presets._lin(level))


def _step(values: list, cur, want_stops: float, stops_of, recording: bool):
    """Next value in `values` toward `want_stops` (brighter > 0), or None.

    While recording: exactly one notch. Otherwise the value closest to the target.
    A move is only made if it actually brings the exposure closer (no ping-pong
    between two values that both miss)."""
    if cur not in values or not values:
        return None
    i = values.index(cur)
    if recording:
        j = i + (1 if want_stops > 0 else -1)
        cands = [values[j]] if 0 <= j < len(values) else []
    else:
        cands = sorted(values, key=lambda v: abs(stops_of(cur, v) - want_stops))[:1]
    for v in cands:
        if v != cur and abs(want_stops - stops_of(cur, v)) < abs(want_stops):
            return v
    return None


def plan_step(props, err: float, recording: bool, max_iso: int, lock_iris: bool):
    """Pick the next (setting, value) for an exposure error, or (None, reason).

    reason: "too_dark" / "too_bright" (out of range), or "closest" (the nearest
    available value is already the best fit)."""
    iso_p, ap_p = props[PROPS["iso"]], props[PROPS["aperture"]]
    isos = sorted(c for c in iso_p.choices if 0 < c < 0xFFFFFF and c <= max_iso)
    aps = sorted(c for c in ap_p.choices if c)
    want = max(min(err, 2.0), -2.0)
    iso_stops = lambda a, b: math.log2(b / a)
    ap_stops = lambda a, b: -2 * math.log2(b / a)       # smaller f-number = brighter
    blocked = True
    if 0 < iso_p.value < 0xFFFFFF:
        v = _step(isos, iso_p.value, want, iso_stops, recording)
        if v is not None:
            return "iso", v
        at_limit = (want > 0 and iso_p.value >= max(isos, default=0)) or (want < 0 and iso_p.value <= min(isos, default=0))
        blocked = at_limit
    if not lock_iris and blocked:
        v = _step(aps, ap_p.value, want, ap_stops, recording)
        if v is not None:
            return "aperture", v
    if not blocked:
        return None, "closest"
    ap_limit = lock_iris or (want > 0 and ap_p.value <= min(aps, default=0)) or (want < 0 and ap_p.value >= max(aps, default=0))
    if not ap_limit:
        return None, "closest"
    return None, ("too_dark" if err > 0 else "too_bright")


class LightFollower:
    def __init__(self, studio):
        self.studio = studio
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.pause_until = 0.0
        self.params = {"over": 1.5, "max_iso": 6400, "lock_iris": False}
        self.status = {"running": False, "message": "", "error": None, "changes": 0, "log": False}

    def start(self, params: dict) -> None:
        self.params.update({k: params[k] for k in ("over", "max_iso", "lock_iris") if k in params})
        self.params["over"] = float(min(max(float(self.params["over"]), 0.0), 3.0))
        self.params["max_iso"] = int(self.params["max_iso"])
        self.params["lock_iris"] = bool(self.params["lock_iris"])
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.status = {"running": True, "message": "Watching the light…", "error": None, "changes": 0, "log": False}
        self.thread = threading.Thread(target=self._loop, daemon=True, name="follow-light")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.status.update(running=False, message="Off")

    def pause(self, seconds: float = 3.0) -> None:
        """Called when a preset is being applied, so the two don't fight."""
        self.pause_until = time.time() + seconds

    def _loop(self) -> None:
        st = self.studio
        ema, off_since, last_change, props, props_at, seen = None, None, 0.0, None, 0.0, -1
        while not self.stop_event.is_set():
            time.sleep(TICK_S)
            st.last_view_req = time.time()          # keep the live-view pump running
            if time.time() < self.pause_until or st.frame is None or st.frame_seq == seen:
                continue
            seen = st.frame_seq
            try:
                if props is None or time.time() - props_at > 3:
                    props, props_at = st.props(max_age=3), time.time()
                mode = props.get(PROPS["exposure_mode"])
                if mode is None or mode.value not in (MOVIE_M, presets.MODE_SQ_M, presets.MODE_M):
                    self.status.update(message="Needs manual exposure: apply a video preset (e.g. LOG) first.", error=None)
                    continue
                pp = props.get(presets.PROP_PICTURE_PROFILE)
                log = bool(pp and pp.value in LOG_PROFILES)
                level, _hi, _lo = presets.brightness(st.frame)
                ema = level if ema is None else ema * 0.7 + level * 0.3
                if time.time() - last_change < SETTLE_S:
                    continue
                err = error_stops(ema, log, self.params["over"])
                self.status.update(error=round(err, 2), log=log)
                if log:
                    now_over = (ema - presets.SLOG3_GREY) / presets.SLOG3_STOP
                    where = f"S-Log3 at {now_over:+.1f} stops over grey (aim {self.params['over']:+.1f})"
                else:
                    where = f"exposure {-err:+.1f} stops from ideal"
                if abs(err) < DEAD_BAND:
                    off_since = None
                    self.status["message"] = "✓ " + where
                    continue
                off_since = off_since or time.time()
                if time.time() - off_since < HOLD_S:
                    self.status["message"] = where + " · checking…"
                    continue
                name, value = plan_step(props, err, bool(st.recording), self.params["max_iso"],
                                        self.params["lock_iris"])
                if name is None and value == "closest":
                    self.status["message"] = "✓ " + where + " (closest setting)"
                    continue
                if name is None:
                    if value == "too_bright":
                        self.status["message"] = (f"Too bright by {abs(err):.1f} stops even at the lowest ISO and "
                                                  f"smallest aperture. Use an ND filter (about ND{2 ** math.ceil(abs(err)):.0f}).")
                    else:
                        self.status["message"] = (f"Too dark by {err:.1f} stops at the ISO limit. Add light"
                                                  + (", or switch to HLG / a normal profile (8-bit log gets noisy when dark)." if log else "."))
                    continue
                st.run(lambda c: c.set(name, value))
                props = st.props(max_age=0)
                props_at = last_change = time.time()
                off_since, ema = None, None
                self.status["changes"] += 1
                pretty = f"ISO {value}" if name == "iso" else f"f/{value / 100:g}"
                self.status["message"] = f"Adjusted → {pretty} ({where})"
            except Exception as exc:  # noqa: BLE001 - keep following; camera may be reconnecting
                self.status["message"] = f"Waiting for the camera… ({exc})"
                props = None
                time.sleep(1.0)
        self.status["running"] = False
