"""DIRECTOR: Claude operates the camera itself through a tool-use loop.

Claude gets a goal ("moody interview, protect the highlights"), looks at the live
view, changes settings, looks again and corrects, for at most MAX_STEPS model
calls. Every change goes into a shared change log with the value it replaced, so
each one (or a whole run) can be undone with one click.

Claude has NO shutter or record tool: photos and recording only ever happen when
the person presses the button in Studio.
"""
from __future__ import annotations

import base64
import threading
import time

from . import assistant, measure, presets

MAX_STEPS = 10

SYSTEM = (
    "You are the camera operator for a professional who grades everything himself in DaVinci Resolve and "
    "Lightroom/Photoshop. Your job is a TECHNICALLY PERFECT, NEUTRAL capture on a Sony ZV-E10, never a look. "
    "Fixed rules: photos are RAW (RAW+JPEG), video is S-Log3 (PP8); never change the picture profile away from "
    "that, never set a WB shift, picture effect, creative style or any colour/contrast setting, never pick a look. "
    "Start with `apply_profile` (it sets the format, neutral colour, shutter for the frame rate, aperture for the "
    "subject, exposure by measurement, focus), then check with `look` and fine-tune only exposure, ISO, aperture, "
    "white balance (Kelvin, to make greys neutral) and focus. Professional standards: S-Log3 skin about +1.5 "
    "stops over mid grey (about 50-58 IRE) with nothing important clipped; RAW photos as bright as possible "
    "without clipping skin or important highlights; lowest practical ISO (noise guard: max 6400 video, 3200 "
    "photo); 180-degree shutter in video; eyes in focus. When the light is not good enough, say plainly what "
    "light to add or move instead of pushing ISO. Judge by the IRE measurements from `look` and "
    "`measure_region`, not by eye. Only use allowed values and give a short reason for every change. Change "
    "several settings per turn, then `look`. You cannot take photos or record. ALWAYS end with `finish` "
    "(never plain text): `summary` at most two plain sentences; `advice` at most three '- ' lines about light, "
    "framing or the subject. You have at most {steps} turns."
)
TOOLS = [
    {"name": "set_setting",
     "description": "Change one camera setting to one of its allowed values (the integer code).",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["name", "value", "reason"],
                      "properties": {"name": {"type": "string"}, "value": {"type": "integer"},
                                     "reason": {"type": "string", "description": "Why, in one short sentence."}}}},
    {"name": "apply_profile",
     "description": ("Run a Studio capture profile (interview, portrait, product, daylight, lowlight): neutral "
                     "colour, RAW / S-Log3, best quality, shutter for the frame rate, aperture for the subject, "
                     "exposure by measurement, focus. Returns a technical verdict. Fine-tune afterwards."),
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["profile", "reason"],
                      "properties": {"profile": {"type": "string", "enum": ["interview", "portrait", "product", "daylight", "lowlight"]},
                                     "reason": {"type": "string"}}}},
    {"name": "autofocus", "description": "Run autofocus with the current focus area.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                      "properties": {"reason": {"type": "string"}}}},
    {"name": "focus_at",
     "description": "Focus on a point of the frame (x, y from 0 to 1, top-left is 0,0).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["x", "y", "reason"],
                      "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                     "reason": {"type": "string"}}}},
    {"name": "look",
     "description": ("Get a fresh live-view frame with IRE measurements (face/skin, 3x3 zones, clipping, "
                     "false-colour coverage) and the current settings."),
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "measure_region",
     "description": "Measure exposure (IRE mean, p90, max, % clipped) inside a box of the current frame (0..1 coordinates).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["x", "y", "w", "h", "what"],
                      "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"},
                                     "h": {"type": "number"}, "what": {"type": "string"}}}},
    {"name": "finish",
     "description": "End the session when the camera is set up.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["summary"],
                      "properties": {"summary": {"type": "string"}, "advice": {"type": "string"}}}},
]


class ChangeLog:
    """Every setting change made by Claude (Director or Lighting Lab), undoable."""

    def __init__(self, studio, settings_view, code_of, order: list[str]):
        self.studio, self.settings_view, self.code_of, self.order = studio, settings_view, code_of, order
        self.entries: list[dict] = []
        self.version = 0
        self._id = 0
        self._lock = threading.Lock()

    def add(self, source: str, kind: str, text: str, reason: str = "", undo: dict | None = None,
            run: int | None = None) -> dict:
        with self._lock:
            self._id += 1
            e = {"id": self._id, "t": time.time(), "source": source, "kind": kind, "text": text,
                 "reason": reason, "undo": undo or None, "undone": False, "run": run}
            self.entries.append(e)
            del self.entries[:-300]
            self.version += 1
            return e

    def settings(self) -> list[dict]:
        return self.settings_view(self.studio.props(max_age=0))

    def snapshot(self) -> dict[str, int]:
        return {s["name"]: s["value"] for s in self.settings() if s.get("writable")}

    def undo(self, ids: list[int]) -> list[str]:
        """Put back the values the given entries replaced (newest first)."""
        targets = sorted((e for e in self.entries if e["id"] in ids and e["undo"] and not e["undone"]),
                         key=lambda e: -e["id"])
        restore: dict[str, int] = {}
        for e in targets:                  # oldest original value wins when entries overlap
            restore.update(e["undo"])
        done, failed = [], []
        for name in sorted(restore, key=lambda n: self.order.index(n) if n in self.order else 99):
            try:
                self.studio.run(lambda c, n=name: c.set(self.code_of(n), int(restore[n])))
                done.append(name)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{name}: {exc}")
        for e in targets:
            e["undone"] = True
        if targets:
            self.add("you", "undo", f"Undid {len(targets)} change(s): " + (", ".join(done) or "nothing"),
                     "; ".join(failed))
        self.version += 1
        return failed

    def apply(self, changes: list[dict], source: str, reason: str = "", run: int | None = None) -> dict:
        """Set validated {name, value} changes, logging each with its old value."""
        cur = {s["name"]: s for s in self.settings()}
        applied, failed = [], []
        for ch in changes:
            s = cur.get(ch.get("name"))
            choice = next((c for c in (s or {}).get("choices", []) if c["value"] == ch.get("value")), None)
            if s is None or choice is None or not s.get("writable"):
                failed.append(f"{ch.get('name')}={ch.get('value')} (not allowed)")
                continue
            if s["value"] == choice["value"]:
                continue
            try:
                self.studio.run(lambda c, n=s["name"], v=choice["value"]: c.set(self.code_of(n), v))
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{s['name']}: {exc}")
                continue
            self.add(source, "change", f"{s.get('friendly') or s['name']}: {s['label']} → {choice['label']}",
                     ch.get("reason") or reason, {s["name"]: s["value"]}, run)
            applied.append(s["name"])
            s["value"], s["label"] = choice["value"], choice["label"]
        return {"applied": applied, "failed": failed}


class Director:
    def __init__(self, studio, log: ChangeLog, follower=None, label=None, run_profile=None):
        self.studio, self.log, self.follower, self.label = studio, log, follower, label
        self.run_profile = run_profile
        self.status = {"running": False, "goal": "", "step": 0, "max_steps": MAX_STEPS,
                       "state": "idle", "summary": "", "advice": "", "error": "", "run": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control ------------------------------------------------------------
    def start(self, goal: str, mode: str = "photo", feature: str = "director") -> None:
        if self.status["running"]:
            raise RuntimeError("The director is already working. Press STOP first.")
        if not assistant.has_key():
            raise RuntimeError("NO_KEY")
        self._stop.clear()
        run = self.status["run"] + 1
        self.status.update(running=True, goal=goal, step=0, state="starting", summary="", advice="",
                           error="", run=run, mode=mode, feature=feature)
        self._thread = threading.Thread(target=self._run, args=(goal, mode, run), daemon=True,
                                        name="director")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.status["running"]:
            self.status["state"] = "stopping"

    # -- helpers ------------------------------------------------------------
    def _pause_follow(self, s: float) -> None:
        if self.follower is not None:
            self.follower.pause(s)

    def _is_log(self, pp) -> bool:
        from . import camprofile
        return camprofile.is_log(camprofile.of(self.studio), pp)

    def _fresh_frame(self) -> bytes:
        """A live-view frame taken after the last change has settled."""
        seq = self.studio.frame_seq
        time.sleep(0.5)
        frame, got = self.studio.latest_frame(seq + 2, timeout=3.0)
        if frame is None or got <= seq:
            frame = self.studio.run(lambda c: c.live_view(), keep_cache=True)
        return frame

    def _settings_text(self, settings: list[dict] | None = None) -> str:
        main, extra = [], []
        for s in settings if settings is not None else self.log.settings():
            if not s.get("writable") or not s.get("choices"):
                continue
            opts = ", ".join(f"{c['value']} ({c['label']})" if c["label"] != str(c["value"]) else str(c["value"])
                             for c in s["choices"])
            name = f"{s['name']} [{s['friendly']}]" if s.get("friendly") else s["name"]
            (extra if s.get("extra") else main).append(
                f"- {name}: currently {s['value']} ({s['label']}). Allowed: {opts}")
        return "\n".join(main) + ("\n\nExtra properties:\n" + "\n".join(extra) if extra else "")

    def _look_content(self) -> list[dict]:
        frame = self._fresh_frame()
        self._last_frame = frame
        settings = self.log.settings()
        pp = next((s["value"] for s in settings if s["name"] == "picture_profile"), 0)
        try:
            text = "Measurements:\n" + measure.summary(measure.analyze(frame, log=self._is_log(pp)), log=self._is_log(pp))
        except Exception as exc:  # noqa: BLE001 - never fail a look on the maths
            level, hi, _lo = presets.brightness(frame)
            text = f"Live view mean {level:.0f}/255, {hi:.1f}% clipped (detailed measurement failed: {exc})."
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(frame).decode()}},
                {"type": "text", "text": text + "\n\nCurrent settings:\n" + self._settings_text(settings)}]

    def _tool(self, name: str, inp: dict, run: int) -> tuple[list[dict] | str, bool]:
        """Run one tool call; returns (tool_result content, is_error)."""
        reason = str(inp.get("reason", ""))[:300]
        if name == "look":
            return self._look_content(), False
        if name == "measure_region":
            frame = getattr(self, "_last_frame", None) or self._fresh_frame()
            st = measure.region_stats(frame, *(float(inp.get(k, 0)) for k in ("x", "y", "w", "h")))
            what = str(inp.get("what", "region"))[:60]
            return (f"{what}: mean {st['mean']} IRE, p10 {st['p10']}, p90 {st['p90']}, max {st['max']}, "
                    f"{st['clip_pct']}% clipped (box {st['box']})."), False
        if name == "set_setting":
            self._pause_follow(30)
            r = self.log.apply([{"name": inp.get("name"), "value": inp.get("value"), "reason": reason}],
                               "director", run=run)
            if r["failed"]:
                return "Not applied: " + "; ".join(r["failed"]), True
            return ("Done." if r["applied"] else "Already at that value."), False
        if name in ("apply_profile", "apply_preset"):
            if self.run_profile is None:
                return "Capture profiles are not available here.", True
            key = inp.get("profile") or inp.get("preset") or ""
            e, v, _exp = self.run_profile(key, self.status.get("mode", "photo"))
            e["run"] = run
            items = "; ".join(f"{i['text']}" + (f" (to do: {i['todo']})" if i["todo"] else "") for i in v["items"])
            return f"Profile applied. Technical verdict: {items}. Call look to check it.", False
        if name == "autofocus":
            self.studio.run(lambda c: (c.autofocus(), c.set("focus_mode", 1) if 1 in c.refresh()[0x500A].choices else None))
            self.log.add("director", "focus", "Autofocus, then locked (MF)", reason, run=run)
            return "Focused.", False
        if name == "focus_at":
            x, y = (min(max(float(inp.get(k, 0.5)), 0.0), 1.0) for k in ("x", "y"))
            self.studio.run(lambda c: (c.focus_at(x, y), time.sleep(0.6), c.set("focus_mode", 1) if 1 in c.refresh()[0x500A].choices else None))
            self.log.add("director", "focus", f"Focus at {x:.2f}, {y:.2f}, then locked (MF)", reason, run=run)
            return "Focused there.", False
        return f"Unknown tool {name}.", True

    # -- the loop -----------------------------------------------------------
    def _run(self, goal: str, mode: str, run: int) -> None:
        assistant.FEATURE.set(self.status.get("feature", "director"))     # usage meter tag (this thread)
        st = self.status
        self.log.add("director", "start", f"Director: {goal or 'best professional result'}", run=run)
        try:
            first = self._look_content()
            first.append({"type": "text", "text": (
                f"Section: {mode} ({'S-Log3 video' if mode == 'video' else 'RAW photos'}). Capture profiles for "
                "apply_profile: interview, portrait, product, daylight, lowlight.\n\n"
                f"Goal: {goal.strip() or 'A technically perfect, neutral capture of this scene.'}")})
            messages = [{"role": "user", "content": first}]
            system = assistant.cam(SYSTEM.format(steps=MAX_STEPS))
            key = assistant._key()
            for step in range(1, MAX_STEPS + 1):
                if self._stop.is_set():
                    raise _Stopped()
                st.update(step=step, state="thinking")
                body = {"model": assistant.MODEL, "max_tokens": 16000, "system": system, "tools": TOOLS,
                        "output_config": {"effort": "medium"}, "fallbacks": "default", "messages": messages}
                data = assistant.call(body, key)
                if self._stop.is_set():
                    raise _Stopped()
                content = data.get("content", [])
                messages.append({"role": "assistant", "content": content})
                note = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                calls = [b for b in content if b.get("type") == "tool_use"]
                if note and calls:
                    self.log.add("director", "note", note[:600], run=run)
                if data.get("stop_reason") == "refusal":
                    raise RuntimeError("Claude declined this request.")
                if not calls:
                    st.update(summary=note)
                    break
                results, finished = [], False
                for b in calls:
                    if self._stop.is_set():
                        raise _Stopped()
                    if b["name"] == "finish":
                        st.update(summary=str(b["input"].get("summary", "")), advice=str(b["input"].get("advice", "")))
                        results.append({"type": "tool_result", "tool_use_id": b["id"], "content": "OK"})
                        finished = True
                        continue
                    st["state"] = {"look": "looking"}.get(b["name"], "adjusting")
                    try:
                        out, is_err = self._tool(b["name"], b.get("input") or {}, run)
                    except Exception as exc:  # noqa: BLE001
                        out, is_err = f"Camera error: {exc}", True
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out,
                                    **({"is_error": True} if is_err else {})})
                messages.append({"role": "user", "content": results})
                if finished:
                    break
            else:
                st["summary"] = st["summary"] or f"Stopped after {MAX_STEPS} turns."
            self.log.add("director", "finish", "Finished: " + (st["summary"] or "done.")[:400], run=run)
            st["state"] = "done"
        except _Stopped:
            st["state"] = "stopped"
            self.log.add("director", "finish", "Stopped by you.", run=run)
        except assistant.AskError as exc:
            st.update(state="error", error=str(exc))
            self.log.add("director", "error", str(exc), run=run)
        except Exception as exc:  # noqa: BLE001
            st.update(state="error", error=str(exc) or exc.__class__.__name__)
            self.log.add("director", "error", st["error"], run=run)
        finally:
            st["running"] = False
            self._pause_follow(2)


class _Stopped(Exception):
    pass
