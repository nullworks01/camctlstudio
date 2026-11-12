"""OPERATOR: decides like a cinematographer, then locks and holds.

- Deciding: reads the scene (subject, light, ratio, colour, flicker…), picks the shot type, sets the
  camera in full manual through a capture profile (base ISO, aperture for the shot, shutter for the
  frame rate and mains, measured exposure, locked WB if measured), verifies, and writes short notes.
- Locked: nothing changes on its own. During a recording nothing changes at all (unless the owner allows
  adjustments while recording).
- Proposal: it keeps measuring and only speaks up when something real changed (light moved by about
  2/3 stop or more for several seconds, the shot type changed, the colour of the light changed). It
  shows one proposal; it is applied between takes (automatically if that setting is on).
It never shoots, records or deletes, and makes no Claude calls in the loop (Claude only decides on
connect, when the shot type changes, or when the owner taps "Analyse again").
"""
from __future__ import annotations

import math
import threading
import json
import time

from . import scene as scenemod

MOVIE_MODES = {0x78050, 0x78051, 0x78052, 0x78053, 0x98059, 0x9805A, 0x9805B, 0x9805C}
LIGHT_CHANGE_STOPS = 0.67
HOLD_S = 5.0                 # a change must last this long before it counts
LIGHT_HOLD_S = 20.0          # a brightness change must stay steady this long (a hand, the filter or the lens cap in
                             # front of the lens is not a light change: 12 Nov 2026 a 6.8-stop spike became a proposal)
SHOT_HOLD_S = 20.0           # a different subject must stay this long (and in every recent reading)
SETTLE_S = 6.0               # after a setup or change: let the camera and live view settle before measuring
COOLDOWN_S = 90.0            # no automatic change within this long of the last one
LEAVE_HOLD_S = 60.0          # a person must be gone this long before "no person" counts (looking down / away is not leaving)
PROPOSAL_GAP_S = 180.0       # at most one proposal per few minutes, never a chain
TOUCH_QUIET_S = 60.0         # no proposal while he is using the app or just changed something
WATCHED = ("exposure_mode", "aperture", "shutter_speed", "iso", "focus_mode", "focus_area", "white_balance",
           "color_temp", "picture_profile")
NICE = {"exposure_mode": "mode", "aperture": "aperture", "shutter_speed": "shutter", "iso": "ISO", "focus_mode": "focus",
        "focus_area": "focus area", "white_balance": "WB", "color_temp": "colour temp", "picture_profile": "picture profile"}
COLOUR_CHANGE = 0.05
RECENT_SETUP_S = 30 * 60


def _stamp_file():
    import os
    from pathlib import Path
    return Path(os.environ.get("APPDATA") or Path.home()) / "camctl" / "last_auto_setup"


def _last_setup_time() -> float:
    try:
        return float(_stamp_file().read_text())
    except (OSError, ValueError):
        return 0.0


FP_KEYS = ("exposure_mode", "aperture", "shutter_speed", "iso", "white_balance", "color_temp", "focus_mode", "picture_profile")
SAME_CAMERA_S = 6 * 3600     # a reconnect / restart within this time with the camera exactly as set up: no new setup


def _fp_file() -> Path:
    return _stamp_file().with_name("last_setup_values.json")


def _save_fp(values: dict) -> None:
    try:
        _fp_file().write_text(json.dumps({k: values.get(k) for k in FP_KEYS}))
    except OSError:
        pass


def _last_setup_values() -> dict:
    """The camera values right after the last setup (kept across restarts)."""
    try:
        return {k: v for k, v in json.loads(_fp_file().read_text()).items() if v is not None}
    except (OSError, ValueError, AttributeError):
        return {}


def _same_as_last_setup(values: dict) -> bool:
    try:
        fp = json.loads(_fp_file().read_text())
    except (OSError, ValueError):
        return False
    return bool(fp) and all(fp.get(k) == values.get(k) for k in FP_KEYS)


def _log_event(text: str, kind: str) -> None:
    """Operator events also go to a file, so what started a setup can be traced after a restart."""
    try:
        f = _stamp_file().parent / "logs" / "operator.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            import datetime as _dt
            fh.write(_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f"  {kind:8s} {text}\n")
    except Exception:  # noqa: BLE001 - a log line must never stop the Operator
        pass


def _mark_setup_time() -> None:
    try:
        f = _stamp_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(time.time()))
    except OSError:
        pass


class AutoFix:
    """The OPERATOR (class name kept for the rest of Studio)."""

    def __init__(self, studio, changelog, analyze, settings_values, follower=None, director=None,
                 setup=None, has_key=lambda: False, goal_extra=lambda: "", scene_fn=None, decide_fn=None,
                 apply_fn=None, prefs=lambda: {}, labels_fn=None):
        """scene_fn(frame) -> scene dict; decide_fn(frame, scene, mode, use_claude) -> decision
        {shot, shot_name, why, directions, entry, verdict, by}; apply_fn(proposal) -> applied list."""
        self.studio, self.log, self.analyze, self.values = studio, changelog, analyze, settings_values
        self.follower, self.director, self.setup, self.has_key = follower, director, setup, has_key
        self.goal_extra, self.scene_fn, self.decide_fn, self.apply_fn, self.prefs = goal_extra, scene_fn, decide_fn, apply_fn, prefs
        self.labels_fn = labels_fn
        self.enabled = True
        self.known: dict = {}                # watched settings as Studio last saw them
        self._mine: dict[str, float] = {}    # setting -> when he changed it in the app
        self._studio_until = 0.0             # Studio itself is changing the camera until then
        self.last_touch = 0.0                # last time he used the app
        self._last_proposal = 0.0
        self.auto_setup = True
        from .capture import ISO_CEILING
        self.max_iso = dict(ISO_CEILING)
        self._quiet_until = 0.0              # no measuring until the camera has settled after a change
        self._last_auto = 0.0                # time of the last automatic change
        self.paused: dict[str, float] = {}
        self.events: list[dict] = []
        self.state = "Waiting for the camera…"
        self.phase = "waiting"                 # waiting | deciding | locked | proposal | off
        self.decision: dict = {}
        self.baseline: dict = {}
        self.proposal: dict | None = None
        self.history: list[dict] = []
        self._eid = self._pid = 0
        self._was_connected = False
        self._set_up_this_connection = False
        self._off_since: dict[str, float] = {}
        self._force = False
        self._override: str | None = None
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    # -- control ------------------------------------------------------------
    def start(self) -> None:
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._loop, daemon=True, name="operator")
            self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def touch(self) -> None:
        """He is using the app: no proposals now or shortly after."""
        self.last_touch = time.time()

    def studio_busy(self, seconds: float = 20.0) -> None:
        """Studio itself is about to change the camera (setup, apply): not a change made on the camera body."""
        self._studio_until = max(self._studio_until, time.time() + seconds)

    def manual(self, name: str) -> None:
        self.touch()
        self._mine[name] = time.time()
        if name in ("iso", "aperture", "focus_mode", "focus_area", "exposure_mode", "shutter_speed",
                    "white_balance", "color_temp"):
            self.paused[name] = time.time()
        if name in ("focus_mode", "focus_area"):         # he focused himself: new focus reference
            self.baseline.update(sharp=None, size=None)
            if self.proposal and self.proposal["kind"] == "focus":
                self.proposal, self.phase = None, "locked"
        if name in ("iso", "aperture", "shutter_speed", "white_balance", "color_temp", "exposure_mode"):
            # his own exposure/colour change is not "the light changed": take a new reference
            self.baseline.update(ire=None, cast=None)
            self._off_since.clear()
            if self.proposal and self.proposal["kind"] in ("light", "colour"):
                self.proposal, self.phase = None, "locked"

    def resume(self) -> None:
        self.paused.clear()

    def analyse_again(self, shot: str | None = None) -> None:
        """the owner tapped 'Analyse again', or picked the shot type himself (override)."""
        self._override = shot
        self._force = True

    def mode(self, values: dict) -> str:
        return "video" if values.get("exposure_mode") in MOVIE_MODES else "photo"

    def status(self) -> dict:
        return {"enabled": self.enabled, "auto_setup": self.auto_setup, "paused": sorted(self.paused),
                "state": self.state, "phase": self.phase, "events": self.events[-12:], "max_iso": self.max_iso,
                "decision": {**{k: self.decision.get(k) for k in ("shot", "shot_name", "why", "directions", "verdict", "by")},
                             "entry": (self.decision.get("entry") or {}).get("id")},
                "proposal": self.proposal, "baseline": self.baseline, "chosen": getattr(self, "chosen", {}), "last_setup": _last_setup_values()}

    def _event(self, text: str, entry: dict | None = None, kind: str = "change") -> None:
        self._eid += 1
        self.events.append({"id": self._eid, "t": time.time(), "text": text, "kind": kind,
                            "entry": entry["id"] if entry else None, "undoable": bool(entry and entry.get("undo"))})
        _log_event(text, kind)
        del self.events[:-40]

    def _busy(self) -> bool:
        return bool(self.director is not None and self.director.status.get("running"))

    # -- deciding -----------------------------------------------------------
    def decide(self, frame: bytes, use_claude: bool) -> None:
        self.studio_busy(120)
        values = self.values()
        mode = self.mode(values)
        self.phase, self.state = "deciding", "Reading the scene…"
        sc = self.scene_fn(frame) if self.scene_fn else {}
        if self._override:
            sc = {**sc, "override": self._override}
        d = self.decide_fn(frame, sc, mode, use_claude) if self.decide_fn else {}
        self._override = None
        self.decision = d
        self.proposal = None
        lc = sc.get("light_colour") or {}
        # the scene was read BEFORE the camera was set up: the new exposure gets a fresh reference
        self.baseline = {"ire": None, "shot": d.get("shot"),
                         "subject": (sc.get("subject") or {}).get("type"),
                         "cast": lc.get("face") if lc.get("face") is not None else lc.get("background"),
                         "t": time.time(), "mode": mode}
        self._off_since.clear()
        self.history = []
        self._quiet_until = time.time() + SETTLE_S
        self._last_auto = time.time()
        self._studio_until = time.time() + SETTLE_S
        self.phase = "locked"
        self.state = f"Locked: {d.get('shot_name', 'set up')}"
        try:
            _save_fp(self.values())                  # what the camera looks like right after this setup
        except Exception:  # noqa: BLE001
            pass
        if d.get("entry"):
            self._event(f"Set up for {d.get('shot_name', 'this shot')}" + (" (Claude)" if d.get("by") == "claude" else ""),
                        d["entry"], kind="setup")

    # -- watching -----------------------------------------------------------
    def _sustained(self, key: str, cond: bool) -> bool:
        return self._sustained_for(key, cond, HOLD_S)

    def _sustained_for(self, key: str, cond: bool, hold: float) -> bool:
        if not cond:
            self._off_since.pop(key, None)
            return False
        self._off_since.setdefault(key, time.time())
        return time.time() - self._off_since[key] >= hold

    def _propose(self, kind: str, text: str, **extra) -> None:
        if self.proposal and self.proposal["kind"] == kind:
            return
        now = time.time()
        if kind == "focus" and self.proposal and self.proposal["kind"] in ("light", "colour"):
            self.proposal = None                   # sharpness matters more: focus replaces a light / colour note
            self._last_proposal = 0.0
        if self.proposal or now - self._last_proposal < PROPOSAL_GAP_S or now - self.last_touch < TOUCH_QUIET_S:
            return                                   # one at a time, a few minutes apart, never while he works
        self._last_proposal = now
        self._pid += 1
        self.proposal = {"id": self._pid, "kind": kind, "text": text, **extra}
        self.phase, self.state = "proposal", text
        self._event("Proposal: " + text, kind="proposal")

    def apply_proposal(self, pid: int | None = None) -> list:
        p = self.proposal
        if not p or (pid is not None and p["id"] != pid):
            return []
        if self.studio.recording and not self.prefs().get("allow_while_recording"):
            return []
        if p["kind"] == "shot":
            self.proposal = None
            self.analyse_again(p.get("shot"))
            return ["re-decide"]
        self.studio_busy(30)
        done = self.apply_fn(p) if self.apply_fn else []
        self._studio_until = time.time() + SETTLE_S
        self.proposal = None
        self.phase, self.state = "locked", "Locked"
        self.baseline["ire"] = None                    # take a new reference after the change
        self._off_since.clear()
        self._quiet_until = time.time() + SETTLE_S
        if p["kind"] == "focus":
            self.baseline.update(sharp=None, size=None)
        if done:
            self._event("Applied: " + p["text"], kind="change")
        return done

    def dismiss(self) -> None:
        self.proposal = None
        self.phase, self.state = "locked", "Locked"
        self.baseline["ire"] = None

    @staticmethod
    def _covered(frame: bytes) -> bool:
        """Mostly covered, black, white or smeared: something is in front of the lens, the picture says nothing."""
        try:
            from PIL import Image, ImageFilter, ImageStat
            import io as _io
            im = Image.open(_io.BytesIO(frame)).convert("L")
            im.thumbnail((160, 160))
            st = ImageStat.Stat(im)
            edges = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).mean[0]
            return st.mean[0] < 8 or st.mean[0] > 247 or st.stddev[0] < 6 or edges < 1.2
        except Exception:  # noqa: BLE001
            return False

    def watch(self, frame: bytes) -> None:
        if time.time() < self._quiet_until:
            return
        if self._covered(frame):                       # never measure light (or anything) on a covered picture
            self._off_since.pop("light", None)
            return
        sc = self.scene_fn(frame) if self.scene_fn else {}
        self.history = (self.history + [sc])[-6:]
        b = self.baseline
        subj = (sc.get("subject") or {}).get("type")
        if subj and b.get("subject") is None:
            b["subject"] = subj
        # brightness is compared on the same kind of subject only (face vs whole frame read differently)
        ire = (sc.get("subject") or {}).get("ire") if (not subj or subj == b.get("subject")) else None
        if b.get("ire") is None:
            if ire is not None:
                b["ire"] = ire
            return
        bx = (sc.get("subject") or {}).get("box")
        moved = bool(bx and b.get("size") and subj == "person" and abs(bx[3] - b["size"]) / b["size"] > 0.25)
        nd = bool(self.prefs().get("nd_on"))       # his ND on the lens: a darker picture is the filter, not the light
        if ire is not None and not moved and not nd:   # a face that came closer reads brighter: not a light change
            stops = math.log2(max((ire / 100) ** 2.2, 1e-4) / max((b["ire"] / 100) ** 2.2, 1e-4))
            side = 1 if stops > 0 else -1
            if self._off_since.get("light") and getattr(self, "_light_side", side) != side:
                self._off_since.pop("light", None)     # it went the other way: not a steady change
            self._light_side = side
            if self._sustained_for("light", abs(stops) >= LIGHT_CHANGE_STOPS, LIGHT_HOLD_S):
                word = "dropped" if stops < 0 else "rose"
                plan = (getattr(self, "plan_text", None) or (lambda p: ""))({"kind": "light", "stops": round(-stops, 2)})
                self._propose("light", f"The light {word} {abs(stops):.1f} stop. "
                              + (f"Apply sets {plan}." if plan else "Nothing to change within your limits."),
                              stops=round(-stops, 2))
                return
        seen = [(h.get("subject") or {}).get("type") for h in self.history]
        steady = len(seen) >= 6 and all(s == subj for s in seen)          # the same new subject in every reading
        hold = LEAVE_HOLD_S if b.get("subject") == "person" else SHOT_HOLD_S   # a lost face keeps the shot
        if subj and b.get("subject") and self._sustained_for("shot", subj != b["subject"] and steady, hold):
            shot_now = scenemod.decide(sc, b.get("mode", "photo"))["shot"]
            what = {"person": "a person", "object": "an object / product", "scene": "a wide scene"}.get(subj, subj)
            self._propose("shot", f"Now I see {what} instead. Set up for a {scenemod.SHOT_NAMES[shot_now]} shot?", shot=shot_now)
            return
        # focus: only a clear, lasting drop in sharpness on the target, or a distance change
        subjd = sc.get("subject") or {}
        sharp, box = subjd.get("sharp"), subjd.get("box")
        if sharp and b.get("sharp") is None:
            b["sharp"] = sharp
        if box and b.get("size") is None:
            b["size"] = box[3]
        if sharp and b.get("sharp") and self._sustained("focus", sharp < b["sharp"] * 0.65):
            self._propose("focus", "Focus looks soft on your subject now. Refocus?")
            return
        if box and b.get("size") and subj == "person" and self._sustained("distance", abs(box[3] - b["size"]) / b["size"] > 0.25):
            self._propose("focus", f"You moved {'closer' if box[3] > b['size'] else 'further away'}. Refocus?")
            return
        lc = sc.get("light_colour") or {}
        cast = lc.get("face") if lc.get("face") is not None else lc.get("background")
        if cast is not None and b.get("cast") is not None and self._sustained("colour", abs(cast - b["cast"]) > COLOUR_CHANGE):
            self._propose("colour", "The colour of the light changed. Measure white balance again with the grey card?")

    def track_changes(self) -> None:
        """History of who changed what: Studio (logged as a setup), him in the app, or the camera body."""
        try:
            now_vals = (self.labels_fn or self.values)() or {}
        except Exception:  # noqa: BLE001
            return
        cur = {k: now_vals.get(k) for k in WATCHED if now_vals.get(k) is not None}
        if not self.known:
            self.known = cur
            return
        diff = [k for k in cur if self.known.get(k) is not None and cur[k] != self.known[k]]
        self.known.update(cur)
        if not diff:
            return
        now = time.time()
        mine = [k for k in diff if now - self._mine.get(k, 0) < 20]
        body = [k for k in diff if k not in mine] if now >= self._studio_until else []
        if mine:
            self._event("You changed " + ", ".join(f"{NICE[k]} to {cur[k]}" for k in mine), kind="you")
        if body:
            self._event("Changed on the camera: " + ", ".join(f"{NICE[k]} {cur[k]}" for k in body), kind="camera")
            self.touch()                               # he is working the camera: hold proposals
            if any(k in ("iso", "aperture", "shutter_speed", "exposure_mode") for k in body):
                self.baseline.update(ire=None)       # new exposure on purpose: new reference, no "light changed"
                self._off_since.pop("light", None)

    def tick(self) -> None:
        st = self.studio
        if st.cam is None or st.frame is None:
            self.phase, self.state = "waiting", "Waiting for the camera…"
            self._was_connected = False
            return
        if not self._was_connected:
            self._was_connected = True
            self._set_up_this_connection = False
        if not self.enabled:
            self.phase, self.state = "off", "Operator off: nothing changes on its own"
            return
        if self._busy():
            return
        rec = bool(st.recording)
        if rec:                                        # hold everything during a take
            if self.proposal and self.prefs().get("allow_while_recording") and self.prefs().get("auto_apply", False):
                self.apply_proposal()
            return
        if self._force:
            self._force = False
            self._event("Setup started by your tap (Analyse again / shot type / apply)", kind="you")
            self.decide(st.frame, use_claude=self.has_key())
            return
        if not self._set_up_this_connection and self.auto_setup:
            self._set_up_this_connection = True
            age = time.time() - _last_setup_time()
            same = age < SAME_CAMERA_S and _same_as_last_setup(self.values())
            last = _last_setup_values() if age < SAME_CAMERA_S else {}
            if last and not same:
                # back within the working day, but not as Studio set it: change nothing, no Claude call; say what
                # differs and leave Analyse again as the one-tap way out (his rule: nothing changes without his press)
                now = self.values()
                keys = [k for k in FP_KEYS if last.get(k) is not None and last.get(k) != now.get(k)]
                names = [NICE.get(k, k) for k in keys]
                two = " and ".join(names[:2]) if names else "some values"
                self._event("The camera came back with different settings: " + ", ".join(
                    f"{NICE.get(k, k)} {last.get(k)} → {now.get(k)}" for k in keys[:4]) + ". Nothing was changed.", kind="setup")
                self.phase, self.state = "changed", f"The camera came back with different settings ({two}). Press Analyse again to set it up."
                if not self.baseline:
                    self.baseline = {"ire": None, "shot": None, "cast": None, "t": time.time(), "mode": self.mode(now)}
                return
            if age >= RECENT_SETUP_S and not same:
                _mark_setup_time()
                self._event("Setup started: first connect" + ("" if age >= SAME_CAMERA_S else " (no saved setup values)"), kind="setup")
                self.decide(st.frame, use_claude=self.has_key())
                return
            if same and age >= RECENT_SETUP_S:
                self._event("Reconnected: the camera is exactly as set up, nothing changed", kind="setup")
            self.phase, self.state = "locked", "Locked (set up recently)"
            if not self.baseline:
                self.baseline = {"ire": None, "shot": None, "cast": None, "t": time.time(), "mode": self.mode(self.values())}
        self.track_changes()
        self.watch(st.frame)
        # only a real change of the light is applied on its own (and not twice in quick succession);
        # a new shot type and focus are proposals he confirms with a tap
        if (self.proposal and self.prefs().get("auto_apply", False) and self.proposal["kind"] == "light"
                and time.time() - self._last_auto >= COOLDOWN_S):
            self._last_auto = time.time()
            self.apply_proposal()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.5)
            try:
                if self.enabled and self.studio.cam is not None:
                    self.studio.last_view_req = max(self.studio.last_view_req, time.time() - 2.5)
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.state = f"Operator paused: {exc}"
                self._stop.wait(2.0)
