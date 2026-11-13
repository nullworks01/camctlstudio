"""One-button video: clip pick-up into the shoot (copy only), silence detection, record hook."""
import subprocess
import time

from camctl import handoff, video


def test_waiting_for_card_then_import_into_shoot(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "Shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    jobs = video.VideoJobs()
    jobs.recording_started()
    assert jobs.status["state"] == "recording"
    found = []
    monkeypatch.setattr(video, "card_clips", lambda since: found)
    jobs.recording_stopped({"quick_draft": False})
    assert jobs.status["state"] == "waiting_clip" and "SD card" in jobs.status["message"]
    clip = tmp_path / "C0007.MP4"
    clip.write_bytes(b"\0" * 2048)
    found.append(clip)                                   # the card appears
    for _ in range(50):
        if jobs.status["state"] == "imported":
            break
        time.sleep(0.1)
    assert jobs.status["state"] == "imported"
    assert clip.exists()                                 # original untouched on the "card"
    assert [p.parent.name for p in (tmp_path / "Shoots").rglob("C0007.MP4")] == ["VIDEO"]


def test_speech_segments_cut_silence(tmp_path):
    import pytest
    if not video.find_ffmpeg():
        pytest.skip(video.FFMPEG_MISSING)          # said loudly by test_libraries::test_ffmpeg_found
    f = tmp_path / "t.mp4"
    lavfi = "sine=f=440:d=1,apad=pad_dur=1.5[a];sine=f=440:d=1[b];[a][b]concat=n=2:v=0:a=1"
    r = subprocess.run([video.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=gray:s=320x180:d=3.5", "-filter_complex", lavfi, "-shortest", "-c:v", "libx264",
                        "-c:a", "aac", str(f)], capture_output=True)
    assert r.returncode == 0, r.stderr
    segs = video.speech_segments(f, min_silence=0.6, pad=0.05)
    assert len(segs) == 2 and segs[0][1] < 1.3 and segs[1][0] > 2.2


def test_an_old_saved_quick_draft_does_nothing(monkeypatch, tmp_path):
    """The automatic clip draft was removed: a stale quick_draft=true in an old prefs file is ignored quietly."""
    monkeypatch.setattr(handoff, "SHOOTS", tmp_path / "Shoots")
    monkeypatch.setattr(handoff, "STATE", tmp_path / "shoot.json")
    jobs = video.VideoJobs()
    clip = tmp_path / "C0009.MP4"
    clip.write_bytes(b"\0" * 1024)
    jobs._imported(clip, {"quick_draft": True, "log": True})
    assert jobs.status["state"] == "imported" and not hasattr(video, "finish") and not hasattr(jobs, "_process")
