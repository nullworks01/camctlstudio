"""Sound report after import: made-up sound files from ffmpeg (tone, noise, clipped bursts, pauses, silence, no
track); read-only on the copy, cached beside the take data, once per clip, cancellable, never Claude."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from camctl import audiocheck, video

FF = video.find_ffmpeg()
pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg not available")


def _make(path: Path, args: list[str]) -> Path:
    subprocess.run([FF, "-hide_banner", "-v", "error", "-y", *args, str(path)], check=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("snd") / "VIDEO"
    d.mkdir()
    lav = lambda expr, dur: ["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={dur}"]
    return {
        "good": _make(d / "good.wav", ["-f", "lavfi", "-i", "anoisesrc=a=0.0004:c=pink:d=8:r=48000",   # a quiet room
                                        "-f", "lavfi", "-i", "aevalsrc='0.25*sin(2*PI*220*t)*lt(mod(t,0.9),0.6)':s=48000:d=8",
                                        "-filter_complex", "amix=inputs=2:normalize=0"]),        # talking with short gaps
        "noisy": _make(d / "noisy.wav", ["-f", "lavfi", "-i", "anoisesrc=a=0.08:c=pink:d=8:r=48000",
                                         "-f", "lavfi", "-i", "aevalsrc='0.25*sin(2*PI*220*t)*lt(mod(t,0.9),0.6)':s=48000:d=8",
                                         "-filter_complex", "amix=inputs=2:normalize=0"]),
        "clipped": _make(d / "clipped.wav", lav("clip(0.2*sin(2*PI*220*t)*(1+9*between(mod(t,2),1,1.1)),-1,1)", 10)),
        "pauses": _make(d / "pauses.wav", lav("0.25*sin(2*PI*220*t)*(lt(t,2)+between(t,5,7)+gte(t,10))", 12)),
        "silent": _make(d / "silent.wav", ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "6"]),
        "notrack": _make(d / "notrack.mp4", ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-t", "3",
                                             "-c:v", "mpeg4"]),
    }


def _line(rep, key):
    return next(x for x in rep["lines"] if x["key"] == key)


def test_a_good_tone(clips):
    r = audiocheck.analyse(clips["good"])
    assert r["sound"] is True and _line(r, "loudness")["word"] == "good for online video"
    assert _line(r, "clipping")["word"] == "none" and _line(r, "pauses")["word"] == "none"
    assert "LUFS" in _line(r, "loudness")["num"]


def test_noise_is_heard(clips):
    assert _line(audiocheck.analyse(clips["noisy"]), "noise")["word"] in ("noticeable", "high")
    assert _line(audiocheck.analyse(clips["good"]), "noise")["word"] == "low"


def test_clipping_with_the_times(clips):
    c = _line(audiocheck.analyse(clips["clipped"]), "clipping")
    assert c["word"] == "clipped in 5 places" and c["num"].startswith("0:01")
    assert [round(t) for t in c["times"]] == [1, 3, 5, 7, 9]


def test_long_pauses_are_counted(clips):
    p = _line(audiocheck.analyse(clips["pauses"]), "pauses")
    assert p["word"] == "2 long pauses" and "editor" in p["advice"]


def test_no_sound_and_no_track(clips):
    assert audiocheck.analyse(clips["silent"])["lines"][0]["word"] == "no sound"
    assert audiocheck.analyse(clips["notrack"])["lines"][0]["word"] == "no sound track"


def test_once_per_clip_on_the_copy_cached_beside_the_takes(clips, tmp_path):
    shoot = tmp_path / "shoot"
    (shoot / "VIDEO").mkdir(parents=True)
    copy = shoot / "VIDEO" / "good.wav"
    copy.write_bytes(clips["good"].read_bytes())
    before = hashlib.sha256(copy.read_bytes()).hexdigest()
    jobs = audiocheck.SoundJobs()
    jobs.add(copy, shoot)
    for _ in range(100):
        if not jobs.status["busy"] and not jobs.queue and audiocheck.load(shoot):
            break
        time.sleep(0.1)
    stored = json.loads((shoot / "NOTES" / "sound.json").read_text(encoding="utf-8"))
    assert stored["good.wav"]["sound"] is True
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == before                # the media is untouched
    assert sorted(p.name for p in (shoot / "VIDEO").iterdir()) == ["good.wav"]    # nothing written beside the media
    jobs.add(copy, shoot)
    assert not jobs.queue                                                           # once per clip


def test_cancel_stops_quietly(clips, tmp_path):
    jobs = audiocheck.SoundJobs()
    for k in ("good", "noisy", "clipped", "pauses"):
        jobs.add(clips[k], tmp_path)
    jobs.cancel()
    time.sleep(0.5)
    assert not jobs.queue and "stopped" in jobs.status["message"].lower()


def test_never_calls_claude():
    src = Path(audiocheck.__file__).read_text(encoding="utf-8")
    assert "assistant" not in src and "anthropic" not in src


def test_takes_page_shows_the_sound_line(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from camctl import handoff, importer, webui
    from test_autopilot import StubCam, _studio_with
    shoot = tmp_path / "shoot"
    audiocheck.save(shoot, "C0001.MP4", {"file": "C0001.MP4", "sound": True, "summary": "Sound: loudness good",
                                         "lines": [{"key": "loudness", "word": "good for online video", "num": "-16 LUFS"}]})
    monkeypatch.setattr(handoff, "current", lambda: {"path": str(shoot), "name": "t"})
    monkeypatch.setattr(importer, "takes", lambda sh=None: [{"id": 1, "kind": "video", "start": 0, "end": 1, "imported": True,
                                                            "files": [str(shoot / "VIDEO" / "C0001.MP4")]}])
    with TestClient(webui.build_app(_studio_with(StubCam()))) as c:
        j = c.get("/api/takes").json()
    assert j["takes"][0]["sound"]["lines"][0]["word"] == "good for online video"
    assert "busy" in j["sound"]
