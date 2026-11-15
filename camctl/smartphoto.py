"""Smart photo mode: shoots by itself only while the owner has armed it in the app.

Fires when the frame is good: a face is found and looks at the camera (frontal-face
detection), it's sharp, well exposed and steady for a moment. A visible counter, a hard
limit per session (default 12, never more than 30) and a cooldown between shots. When the
session ends the best 3 are picked (sharpness + exposure) and copied to PICKS in the shoot.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import handoff

HARD_LIMIT = 30
COOLDOWN_S = 2.5
STEADY_FRAMES = 3


class SmartPhoto:
    def __init__(self, studio, analyze, shoot):
        """analyze(frame) -> coach dict; shoot() -> take_photo() result."""
        self.studio, self.analyze, self.shoot = studio, analyze, shoot
        self.status = {"armed": False, "shots": 0, "limit": 12, "reason": "", "picks": [], "scores": []}
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._boxes: list = []
        self._last = 0.0

    def arm(self, limit: int = 12) -> None:
        if self.status["armed"]:
            return
        self.status.update(armed=True, shots=0, limit=max(1, min(int(limit), HARD_LIMIT)),
                           reason="Waiting for a sharp, well-exposed frame with your face to the camera…",
                           picks=[], scores=[])
        self._stop.clear()
        self._boxes.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="smart-photo")
        self.thread.start()

    def disarm(self) -> None:
        self._stop.set()

    def ready(self, a: dict) -> tuple[bool, str]:
        by = {c["key"]: c for c in a.get("checks", [])}
        o = a.get("overlays", {})
        if not o.get("subject_is_face"):
            self._boxes.clear()
            return False, "Looking for your face (look at the camera)…"
        ex, fo = by.get("exposure", {}), by.get("focus", {})
        if ex.get("status") != "ok":
            return False, "Waiting for good exposure: " + ex.get("label", "")
        if fo.get("status") != "ok":
            return False, "Waiting for sharp focus on your face…"
        box = o.get("subject")
        self._boxes = (self._boxes + [box])[-STEADY_FRAMES:]
        if len(self._boxes) < STEADY_FRAMES:
            return False, "Hold still…"
        xs = [b[0] + b[2] / 2 for b in self._boxes]
        ys = [b[1] + b[3] / 2 for b in self._boxes]
        if max(xs) - min(xs) > 0.03 or max(ys) - min(ys) > 0.03:
            return False, "Hold still…"
        return True, "Shooting…"

    def _score(self, a: dict) -> float:
        by = {c["key"]: c for c in a.get("checks", [])}
        sharp = by.get("focus", {}).get("ratio", 1.0) or 1.0
        stops = abs(by.get("exposure", {}).get("stops", 0.0) or 0.0)
        return round(sharp * 10 - stops * 4, 3)

    def _loop(self) -> None:
        st = self.status
        seen = -1
        try:
            while not self._stop.is_set() and st["shots"] < st["limit"]:
                time.sleep(0.4)
                self.studio.last_view_req = time.time()          # keep frames coming
                if self.studio.frame is None or self.studio.frame_seq == seen:
                    continue
                seen = self.studio.frame_seq
                if time.time() - self._last < COOLDOWN_S:
                    continue
                a = self.analyze(self.studio.frame)
                ok, why = self.ready(a)
                st["reason"] = why
                if not ok:
                    continue
                r = self.shoot()
                self._last = time.time()
                self._boxes.clear()
                st["shots"] += 1
                st["scores"].append({"take": r.get("take"), "files": r.get("shoot", []), "score": self._score(a)})
                st["reason"] = f"Shot {st['shots']} of {st['limit']}."
        except Exception as exc:  # noqa: BLE001
            st["reason"] = f"Stopped: {exc}"
        finally:
            try:
                self._pick()                                  # picks first: "armed: False" means the result is ready
            except Exception as exc:  # noqa: BLE001
                st["reason"] = f"Stopped: {exc}"
            st["armed"] = False

    def _pick(self) -> None:
        best = sorted(self.status["scores"], key=lambda s: -s["score"])[:3]
        sh = handoff.current()
        picks = []
        if sh:
            dst = Path(sh["path"]) / "PICKS"
            dst.mkdir(exist_ok=True)
            for s in best:
                for f in s["files"]:
                    p = Path(f)
                    if p.exists():
                        shutil.copy2(p, dst / p.name)          # copy; originals stay in place
                        picks.append(p.name)
        self.status["picks"] = picks
        if self.status["shots"]:
            self.status["reason"] = (f"Done: {self.status['shots']} shot(s), best {len(best)} copied to PICKS."
                                     if picks else f"Done: {self.status['shots']} shot(s).")
