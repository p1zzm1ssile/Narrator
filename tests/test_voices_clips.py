"""Voices from several clips, and finding the speakers in a video (Whisper and the voice encoder stood in)."""

import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

# Two "people": a low and a high tone, taking turns every 3 seconds, 1 second of silence between turns
TURNS = [(0, 220), (4, 440), (8, 220), (12, 440), (16, 220), (20, 440)]


def _make_video(path):
    sr = 24000
    audio = np.zeros(sr * 24, dtype=np.float32)
    for start, hz in TURNS:
        t = np.arange(sr * 3) / sr
        audio[start * sr:(start + 3) * sr] = 0.3 * np.sin(2 * np.pi * hz * t)
    wav = path.with_suffix(".wav")
    sf.write(wav, audio, sr)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=24",
                    "-i", str(wav), "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)
    return path


def _pitch(wav, sr):
    spectrum = np.abs(np.fft.rfft(wav))
    return np.fft.rfftfreq(len(wav), 1 / sr)[np.argmax(spectrum)]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from core import config, speakers
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "VOICES_DIR", tmp_path / "voices")
    config.ensure_dirs()
    config.load()

    class FakeWhisper:
        def transcribe(self, audio, **kw):
            segs = [SimpleNamespace(start=s + 0.05, end=s + 2.95, text=f"Line at {s} seconds.") for s, _ in TURNS]
            return iter(segs), None

    monkeypatch.setattr(speakers, "_whisper", lambda: FakeWhisper())

    def fake_embed(wavs, sr):
        out = []
        for w in wavs:
            v = np.array([1.0, 0.1, 0.0]) if _pitch(w, sr) < 330 else np.array([0.1, 1.0, 0.0])
            v = np.pad(v, (0, 253))
            out.append(v / np.linalg.norm(v))
        return np.array(out, dtype=np.float32)

    monkeypatch.setattr(speakers, "embed_wavs", fake_embed)
    import main
    return TestClient(main.app), tmp_path


def _wait(client, aid):
    for _ in range(600):
        a = client.get(f"/api/speakers/{aid}").json()
        if a["status"] != "working":
            return a
        time.sleep(0.1)
    raise AssertionError(a)


def test_find_speakers_in_a_video_and_make_a_voice(app):
    client, tmp = app
    video = _make_video(tmp / "scene.mp4")
    with open(video, "rb") as f:
        aid = client.post("/api/speakers", files={"file": ("scene.mp4", f, "video/mp4")}).json()["id"]
    a = _wait(client, aid)
    assert a["status"] == "done", a
    assert len(a["speakers"]) == 2
    low = {s["start"] // 1 for s in a["segments"] if s["speaker"] == a["speakers"][0]["id"]}
    assert low in ({0, 8, 16}, {4, 12, 20})  # each person's lines are grouped together
    assert not (tmp / "analysis" / aid / "source.mp4").exists()  # only the sound is kept

    sample = client.get(f"/api/speakers/{aid}/speaker/0")
    assert sample.status_code == 200 and sample.headers["content-type"] == "audio/wav"
    assert client.get(f"/api/speakers/{aid}/line/0").status_code == 200

    # Group into one person, then back into two
    assert len(client.post(f"/api/speakers/{aid}/regroup", json={"speakers": 1}).json()["speakers"]) == 1
    a = client.post(f"/api/speakers/{aid}/regroup", json={"speakers": 2}).json()

    sp = a["speakers"][0]
    r = client.post(f"/api/speakers/{aid}/voice", json={"lines": sp["best"], "name": "Captain Reyes", "gender": "male"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "chatterbox:captain-reyes"
    clips = sorted((tmp / "voices" / "captain-reyes").glob("clip-*.wav"))
    assert len(clips) == 3  # three separate turns, three clips
    for c in clips:  # every clip is that one person's pitch
        audio, sr = sf.read(c)
        assert abs(_pitch(audio, sr) - (220 if 0 in low else 440)) < 20
    assert (tmp / "voices" / "captain-reyes.wav").exists()
    lib = {v["id"]: v for v in client.get("/api/voices").json()["chatterbox"]}
    assert lib["chatterbox:captain-reyes"]["clips"] == 3

    # Too little speech is refused
    r = client.post(f"/api/speakers/{aid}/voice", json={"lines": [], "name": "X"})
    assert r.status_code == 400


def test_voice_from_several_clips_including_a_video(app):
    client, tmp = app
    sr = 24000
    t = np.arange(sr * 4) / sr
    a = tmp / "a.wav"
    sf.write(a, (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)
    video = _make_video(tmp / "b.mp4")
    with open(a, "rb") as fa, open(video, "rb") as fb:
        r = client.post("/api/voices", data={"name": "Mira", "gender": "female"},
                        files=[("files", ("a.wav", fa, "audio/wav")), ("files", ("b.mp4", fb, "video/mp4"))])
    assert r.status_code == 200, r.text
    assert len(list((tmp / "voices" / "mira").glob("clip-*.wav"))) == 2
    combined, _ = sf.read(tmp / "voices" / "mira.wav")
    assert 20 < len(combined) / sr <= 40  # trimmed, joined, capped

    with open(a, "rb") as fa:
        r = client.post("/api/voices/mira/clips", files=[("files", ("c.wav", fa, "audio/wav"))])
    assert r.json()["clips"] == 3
    assert client.post("/api/voices/nobody/clips", files=[("files", ("c.wav", b"x", "audio/wav"))]).status_code == 404

    # A file with no sound is refused and leaves nothing behind
    (tmp / "bad.txt").write_text("not audio")
    with open(tmp / "bad.txt", "rb") as f:
        r = client.post("/api/voices", data={"name": "Bad"}, files=[("files", ("bad.txt", f, "text/plain"))])
    assert r.status_code == 400
    assert not (tmp / "voices" / "bad.wav").exists()

    client.delete("/api/voices/mira")
    assert not (tmp / "voices" / "mira").exists() and not (tmp / "voices" / "mira.wav").exists()


def test_old_single_file_voice_keeps_working_when_clips_are_added(app):
    client, tmp = app
    from core import voices
    sr = 24000
    t = np.arange(sr * 5) / sr
    sf.write(tmp / "voices" / "old.wav", (0.2 * np.sin(2 * np.pi * 300 * t)).astype(np.float32), sr)
    voices._save_meta("old", "Old voice", "male", "")
    with open(tmp / "voices" / "old.wav", "rb") as f:
        data = f.read()
    r = client.post("/api/voices/old/clips", files=[("files", ("new.wav", data, "audio/wav"))])
    assert r.json()["clips"] == 2  # the original file became the first clip


def test_group_by_count_and_threshold():
    from core import speakers
    rng = np.random.default_rng(1)
    a, b = rng.normal(size=256), rng.normal(size=256)
    embeds = []
    for i in range(10):
        base = a if i % 2 == 0 else b
        v = base + rng.normal(scale=0.2, size=256)
        embeds.append(v / np.linalg.norm(v))
    segs = [{"start": i * 3.0, "end": i * 3.0 + 2, "index": i} for i in range(10)]
    labels = speakers.group(np.array(embeds), segs)
    assert labels[0::2] == [labels[0]] * 5 and labels[1::2] == [labels[1]] * 5 and labels[0] != labels[1]
    assert len(set(speakers.group(np.array(embeds), segs, wanted=1))) == 1
