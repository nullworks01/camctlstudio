"""Automation for camctl Studio: timelapse, bracketing, focus stacking, sequences.

Jobs run in a background thread and take the camera lock only for each
action, so the live view keeps updating between shots. stop() ends a job
between steps. Nothing starts on its own: the user presses Start.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from .drivers.sony import PROPS
from .presets import shutter_seconds


class Stopped(Exception):
    pass


class Automation:
    def __init__(self, studio, save_files: Callable, apply_preset: Callable):
        self.studio = studio
        self.save_files = save_files          # (files) -> list of saved names
        self.apply_preset = apply_preset      # (cam, key, look) -> report
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.status = {"running": False, "kind": None, "step": 0, "total": 0, "message": "", "files": [], "log": []}

    # ------------------------------------------------------------ control
    def start(self, kind: str, params: dict, on_done: Callable | None = None) -> None:
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Another automation is already running. Stop it first.")
        runner = {"timelapse": self._timelapse, "bracket": self._bracket,
                  "focusstack": self._focus_stack, "burst": self._burst, "sequence": self._sequence}.get(kind)
        if runner is None:
            raise ValueError(f"unknown automation '{kind}'")
        self.stop_event.clear()
        self.status = {"running": True, "kind": kind, "step": 0, "total": 0, "message": "Starting…",
                       "files": [], "log": [], "started": time.time()}
        self.on_done = on_done
        self.thread = threading.Thread(target=self._run, args=(runner, params), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.status["message"] = "Stopping after the current step…"

    def _run(self, runner, params):
        ok = False
        try:
            runner(params)
            ok = not self.stop_event.is_set()
            self._log("Finished." if ok else "Stopped.")
        except Stopped:
            self._log("Stopped. Everything shot so far is kept.")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Stopped: {exc}. Everything shot so far is kept.")
            self.status["error"] = str(exc)
            traceback.print_exc()
        finally:
            cb = getattr(self, "on_done", None)
            if cb is not None:
                try:
                    self.status["merging"] = True
                    cb(self.status, ok)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"Couldn't merge: {exc}")
                    self.status["error"] = str(exc)
                finally:
                    self.status["merging"] = False
            self.status["running"] = False

    # ------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        self.status["message"] = msg
        self.status["log"] = (self.status["log"] + [time.strftime("%H:%M:%S ") + msg])[-60:]

    def _wait(self, seconds: float) -> None:
        if self.stop_event.wait(max(0.0, seconds)):
            raise Stopped()

    def _check(self) -> None:
        if self.stop_event.is_set():
            raise Stopped()

    def _cam(self, fn):
        return self.studio.run(fn)

    def _shoot(self, label: str, required: bool = False) -> None:
        self._check()
        files = self._cam(lambda c: c.capture(timeout=20, af=False))
        names = self.save_files(files)
        self.status["files"] += names
        if required and not names:
            raise RuntimeError(f"{label}: the camera did not take the picture")
        self._log(f"{label}: saved {', '.join(names) if names else 'nothing (photo on card only)'}")

    # ------------------------------------------------------------ jobs
    def _timelapse(self, p):
        interval = max(2.0, float(p.get("interval", 10)))
        count = max(1, min(int(p.get("count", 30)), 5000))
        self.status["total"] = count
        self._log(f"Timelapse: {count} shots every {interval:g} s; first in {float(p.get('delay', 0)):g} s")
        self._wait(float(p.get("delay", 0)))
        nxt = time.time()
        for i in range(count):
            self.status["step"] = i + 1
            self._shoot(f"Shot {i + 1}/{count}")
            nxt += interval
            if i + 1 < count:
                self._wait(nxt - time.time())

    def _bracket(self, p):
        """Exposure bracketing by shutter speed (keeps aperture and ISO, as pros do)."""
        frames = int(p.get("frames", 3))
        if frames not in (3, 5, 7, 9):
            raise ValueError("frames must be 3, 5, 7 or 9")
        ev = float(p.get("ev", 1.0))
        props = self._cam(lambda c: c.refresh())
        sh = props[PROPS["shutter_speed"]]
        base = shutter_seconds(sh.value)
        if base is None:
            raise RuntimeError("Set a fixed shutter speed (Manual mode) first.")
        choices = [c for c in sh.choices if shutter_seconds(c)]
        offsets = [ev * (i - frames // 2) for i in range(frames)]
        self.status["total"] = frames
        self._log(f"Bracketing {frames} frames at {ev:g} EV steps around {base:g}s")
        try:
            for i, off in enumerate(offsets):
                self.status["step"] = i + 1
                want = base * (2 ** off)
                val = min(choices, key=lambda c: abs(shutter_seconds(c) / want - 1))
                self._cam(lambda c, v=val: c.set("shutter_speed", v))
                self._shoot(f"Frame {i + 1}/{frames} ({off:+g} EV)")
        finally:
            self._cam(lambda c: c.set("shutter_speed", sh.value))
            self._log("Shutter speed restored.")

    def _fpos(self):
        """The lens focus position the camera reports (0xD24C: 0 nearest .. 100 infinity), or None."""
        try:
            v = self._cam(lambda c: getattr(c.refresh().get(0xD24C), "value", None))
            return int(v) if isinstance(v, int) else None
        except Exception:  # noqa: BLE001
            return None

    def _focus_to(self, target: int) -> int | None:
        """Drive the focus to a position, closing on the read-back (coarse, then fine steps)."""
        for step, tol in ((7, 12), (3, 4), (1, 1)):
            for _ in range(40):
                p = self._fpos()
                if p is None:
                    return None
                gap = target - p
                if abs(gap) <= tol:
                    break
                self._cam(lambda c, s=(step if gap > 0 else -step): c.focus_step(s))
                time.sleep(0.3)
        return self._fpos()

    def _focus_stack(self, p):
        """Focus stacking on a still object: lock everything, shoot from near to far, then put the focus back where it
        was before the series (by its reported position, not by counting steps: 12 Nov 2026 it ended at an end twice).
        With `start` and `end` (focus positions measured by "Check the object") the frames are spread evenly across the
        object; without them: `near` big steps toward the closest focus first, then `step` per frame."""
        shots = max(2, min(int(p.get("shots", 8)), 100))
        step = max(1, min(int(p.get("step", 3)), 7))
        near = max(0, min(int(p.get("near", 0)), 30))
        start, end = p.get("start"), p.get("end")
        snap = self._lock()
        self.status["total"] = shots
        before = self._fpos()
        planned = start is not None and end is not None and before is not None
        targets = [round(start + (end - start) * i / (shots - 1)) for i in range(shots)] if planned else []
        self._log(f"Focus stack: {shots} shots, " + (f"positions {targets}" if planned else f"step {step} (near → far)"))
        self.status["positions"] = []
        moved = 0
        try:
            self._cam(lambda c: c.set("focus_mode", 1))       # MF for the whole series
            if planned:
                self._focus_to(targets[0])
            else:
                for _ in range(near):
                    self._cam(lambda c: c.focus_step(-7))
                    moved -= 7
                if near:
                    self._wait(0.6)
            for i in range(shots):
                self.status["step"] = i + 1
                self.status["positions"].append(self._fpos())
                self._shoot(f"Slice {i + 1}/{shots}", required=True)
                if i + 1 < shots:
                    if planned:
                        self._focus_to(targets[i + 1])
                    else:
                        self._cam(lambda c: c.focus_step(step))
                        moved += step
                        self._wait(0.6)
        finally:
            try:                                           # back to where focus was before the series
                if before is not None:
                    back = self._focus_to(before)
                    self._log(f"Focus back at {back} (was {before}).")
                else:
                    left = -moved
                    while left:
                        s = max(-7, min(7, left))
                        self._cam(lambda c, s=s: c.focus_step(s))
                        left -= s
            except Exception:  # noqa: BLE001
                self._log("Couldn't put the focus back exactly: tap the picture to focus again.")
            self._restore(snap)

    def _burst(self, p):
        """A burst for a clean night shot / long exposure: every setting locked, nothing changes between frames."""
        shots = max(2, min(int(p.get("shots", 8)), 64))
        snap = self._lock()
        self.status["total"] = shots
        self._log(f"Burst: {shots} photos, settings locked")
        try:
            for i in range(shots):
                self.status["step"] = i + 1
                self._shoot(f"Frame {i + 1}/{shots}", required=True)
        finally:
            self._restore(snap)

    # settings that must be exactly the same before and after a series
    LOCK = ("exposure_mode", "aperture", "shutter_speed", "iso", "white_balance", "focus_mode")

    def _lock(self) -> dict:
        props = self._cam(lambda c: c.refresh())
        snap = {}
        for name in self.LOCK:
            pr = props.get(PROPS.get(name, name)) if isinstance(PROPS.get(name, name), int) else None
            if pr is not None:
                snap[name] = pr.value
        for code in (0xD20F, 0xD22C):                      # colour temperature, focus area
            if props.get(code) is not None:
                snap[code] = props[code].value
        self.status["locked"] = {str(k): v for k, v in snap.items()}
        return snap

    def _restore(self, snap: dict) -> None:
        props = self._cam(lambda c: c.refresh())
        changed = []
        for key, val in snap.items():
            code = PROPS.get(key, key)
            cur = props.get(code)
            if cur is not None and cur.value != val:
                try:
                    self._cam(lambda c, k=key, v=val: c.set(k, v))
                    changed.append(str(key))
                except Exception:  # noqa: BLE001
                    self._log(f"Couldn't restore {key}.")
        self._log("Settings restored." + (f" ({', '.join(changed)})" if changed else ""))

    def _sequence(self, p):
        steps = p.get("steps", [])[:200]
        repeat = max(1, min(int(p.get("repeat", 1)), 1000))
        self.status["total"] = len(steps) * repeat
        n = 0
        for r in range(repeat):
            for st in steps:
                n += 1
                self.status["step"] = n
                self._check()
                kind = st.get("type")
                if kind == "wait":
                    self._log(f"Wait {float(st.get('seconds', 1)):g} s")
                    self._wait(float(st.get("seconds", 1)))
                elif kind == "shoot":
                    self._shoot(f"Step {n}: photo")
                elif kind == "preset":
                    rep = self._cam(lambda c, s=st: self.apply_preset(c, s["preset"], s.get("look")))
                    self._log(f"Step {n}: preset {rep['preset']}" + (f" + {rep['look']}" if rep.get("look") else ""))
                elif kind == "set":
                    target = int(st["code"], 16) if isinstance(st.get("code"), str) else st.get("code", st.get("name"))
                    self._cam(lambda c, t=target, s=st: c.set(t, int(s["value"])))
                    self._log(f"Step {n}: set {st.get('name') or st.get('code')} = {st['value']}")
                elif kind == "focus":
                    self._cam(lambda c, s=st: c.focus_at(float(s.get("x", 0.5)), float(s.get("y", 0.5))))
                    self._log(f"Step {n}: focus at {st.get('x')},{st.get('y')}")
                elif kind in ("record_start", "record_stop"):
                    want = kind == "record_start"
                    if self.studio.recording != want:
                        self._cam(lambda c: c.movie_record(True))
                        self.studio.recording = want
                        self.studio.rec_since = time.time() if want else 0.0
                    self._log(f"Step {n}: recording {'started' if want else 'stopped'}")
                else:
                    raise ValueError(f"unknown step type '{kind}'")
        if self.studio.recording:
            self._log("Note: recording is still running (add a 'record_stop' step to end it).")
