"""A whole story through the API, with a stand-in voice so no GPU or model download is needed."""

import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

EXAMPLE = Path(__file__).parent.parent / "examples" / "the-lighthouse.txt"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from core import config, engines, jobs
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "VOICES_DIR", tmp_path / "voices")
    config.ensure_dirs()
    config.load()
    config.update({"whisper_mode": "off"})

    calls = []

    def fake_speak(text, voice, p, seed=None):
        calls.append((text, voice, p))
        seconds = max(0.6, len(text) / 15)
        t = np.arange(int(engines.SAMPLE_RATE * seconds)) / engines.SAMPLE_RATE
        pitch = 180 + (hash(voice) % 100)
        return (0.2 * np.sin(2 * np.pi * pitch * t)).astype(np.float32)

    monkeypatch.setattr(engines, "speak", fake_speak)
    jobs.start_worker()
    import main
    return TestClient(main.app), calls


def wait(client, job_id, statuses, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"still {job['status']}: {job['message']}")


@pytest.mark.parametrize("fmt", ["m4b", "mp3"])
def test_story_to_audiobook(app, fmt):
    client, calls = app
    with open(EXAMPLE, "rb") as f:
        r = client.post("/api/jobs", data={"title": "The Lighthouse", "format": fmt, "review": "1"},
                        files={"file": ("the-lighthouse.txt", f, "text/plain")})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    job = wait(client, job_id, ("review", "failed"))
    assert job["status"] == "review", job
    script = client.get(f"/api/jobs/{job_id}/script").json()
    assert set(script["characters"]) == {"Mira", "Tom"}

    # Recast Tom and change a feeling, as the review page does
    r = client.put(f"/api/jobs/{job_id}/script", json={
        "characters": {"Tom": {"voice": "kokoro:bm_george"}},
        "segments": [{"id": 2, "emotion": "fearful", "intensity": 0.9}],
    })
    assert r.status_code == 200
    assert client.post(f"/api/jobs/{job_id}/render").status_code == 200
    job = wait(client, job_id, ("done", "failed"))
    assert job["status"] == "done", job
    assert any(v == "kokoro:bm_george" for _, v, _ in calls)

    audio = client.get(f"/api/jobs/{job_id}/audio")
    assert audio.status_code == 200
    out = Path(EXAMPLE).parent / f"_test.{fmt}"
    out.write_bytes(audio.content)
    try:
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(out)],
            capture_output=True, text=True, check=True).stdout)
    finally:
        out.unlink()
    titles = [c["tags"]["title"] for c in probe["chapters"]]
    assert titles == ["Chapter 1: The Light Goes Out", "Chapter 2: The Climb"]
    assert float(probe["format"]["duration"]) > 10

    # Rendering again re-uses every clip
    before = len(calls)
    client.post(f"/api/jobs/{job_id}/render")
    wait(client, job_id, ("done", "failed"))
    assert len(calls) == before


def test_bad_job_id(app):
    client, _ = app
    assert client.get("/api/jobs/../../etc").status_code == 404
    assert client.get("/api/jobs/abcdefabcdef").status_code == 404


def test_tagged_story_skips_llm_and_renders(app):
    client, calls = app
    tagged = EXAMPLE.parent / "the-lighthouse-tagged.txt"
    with open(tagged, "rb") as f:
        r = client.post("/api/jobs", data={"review": "0"}, files={"file": ("the-lighthouse-tagged.txt", f, "text/plain")})
    job = r.json()
    assert job["title"] == "The Lighthouse Tagged"
    job = wait(client, job["id"], ("done", "failed"))
    assert job["status"] == "done", job
    script = client.get(f"/api/jobs/{job['id']}/script").json()
    assert script["pretagged"] and set(script["characters"]) == {"Mira", "Tom"}
    spoken = [c[0] for c in calls]
    assert "In this?" in spoken and not any("[" in t for t in spoken)
