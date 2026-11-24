"""Narrator: turns stories into audiobooks with a cast of expressive voices.

Web UI and API on port 9895 (WEB_PORT). See README.md for the API.
"""

import os

# Chatterbox prints a "Sampling: 19%" bar for every line, which stops early when
# the line ends and looks stuck. Narrator logs its own progress instead.
os.environ.setdefault("TQDM_DISABLE", "1")

import base64
import io
import re
import secrets
import shutil
import tempfile
from pathlib import Path

import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import checker, config, engines, jobs, story, tagger, voices
from core import speakers as speakers_mod
from utils.logger import log, setup

__version__ = "0.1.2"

WEB_DIR = Path(__file__).parent / "web"
PASSWORD = os.environ.get("WEB_PASSWORD", "")
MAX_STORY_BYTES = 20_000_000

app = FastAPI(title="Narrator", version=__version__)


@app.middleware("http")
async def password_check(request: Request, call_next):
    """With WEB_PASSWORD set, every page and API call needs it (user name can be anything)."""
    if PASSWORD and request.url.path != "/api/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, PASSWORD)
            except (ValueError, UnicodeDecodeError):
                ok = False
        elif header.lower().startswith("bearer "):
            ok = secrets.compare_digest(header[7:], PASSWORD)
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Narrator"'})
    return await call_next(request)


def _job_or_404(job_id):
    try:
        return jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "No story with that id")


# ----- health and settings -----

@app.get("/api/health")
def health():
    return {"ok": shutil.which("ffmpeg") is not None, "version": __version__}


@app.get("/api/status")
def status():
    s = config.get()
    gpu = None
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            gpu = {"name": torch.cuda.get_device_name(0), "free_gb": round(free / 1e9, 1),
                   "total_gb": round(total / 1e9, 1)}
    except Exception:  # noqa: BLE001
        pass
    return {
        "version": __version__, "gpu": gpu,
        "chatterbox_loaded": engines.chatterbox.model is not None,
        "ollama": bool(s["ollama_url"]), "kokoro": bool(s["kokoro_url"]),
        "whisper": s["whisper_mode"], "rendering": jobs._current["job"], "casting": jobs._current["tagging"],
    }


@app.get("/api/settings")
def get_settings():
    return config.get()


@app.put("/api/settings")
def put_settings(changes: dict):
    try:
        return config.update(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/test/{service}")
def test_service(service: str):
    """Check that Ollama, Kokoro or Whisper answers, using the saved settings."""
    import requests
    s = config.get()
    try:
        if service == "ollama":
            if not s["ollama_url"]:
                return {"ok": False, "message": "No address set"}
            r = requests.get(f"{s['ollama_url']}/api/tags", timeout=10)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            has = any(m == s["ollama_model"] or m.split(":")[0] == s["ollama_model"] for m in models)
            msg = f"Connected. {len(models)} model(s)." + ("" if has else f" {s['ollama_model']} isn't pulled yet: run ollama pull {s['ollama_model']}")
            return {"ok": has, "message": msg, "models": models}
        if service == "kokoro":
            v = engines.kokoro.voices()
            return {"ok": bool(v), "message": f"Connected. {len(v)} voices.", "voices": v}
        if service == "whisper":
            if s["whisper_mode"] == "off":
                return {"ok": False, "message": "Whisper check is off"}
            audio = engines.kokoro.speak("Testing one two three.", "af_heart", engines.params("neutral", 0.5, 1)) \
                if s["kokoro_url"] else None
            if audio is None:
                return {"ok": True, "message": "Whisper loads when the first line is rendered"}
            heard = checker.transcribe(audio, engines.SAMPLE_RATE)
            return {"ok": True, "message": f"Whisper heard: {heard!r}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
    raise HTTPException(404, "Unknown service")


# ----- voices -----

@app.get("/api/voices")
def list_voices():
    names = []
    try:
        names = engines.kokoro.voices()
    except Exception as e:  # noqa: BLE001
        log.debug(f"Kokoro voices unavailable: {e}")
    kokoro = [voices.kokoro_info(v) for v in names]
    return {"chatterbox": voices.library(names), "kokoro": kokoro, "emotions": tagger.EMOTIONS}


@app.get("/api/voices/sample")
def voice_sample(voice: str):
    """A short sample of any voice, e.g. ?voice=kokoro:af_bella. Made once, then kept in /config/samples."""
    if not voice.startswith(("kokoro:", "chatterbox:")):
        raise HTTPException(400, "Voice should start with kokoro: or chatterbox:")
    path = voices.sample_path(voice)
    if not path.exists():
        engine, _, key = voice.partition(":")
        if engine == "kokoro":
            name = voices.kokoro_info(key)["name"].split("+")[0]
        else:
            name = voices.copy_info(key[2:])["name"] if voices.is_copy(key) else \
                next((v["name"] for v in voices.library() if v["id"] == voice), key)
            if name == "Chatterbox default":
                name = "the Chatterbox voice"
        text = voices.SAMPLE_TEXT.format(name=name)
        try:
            audio = engines.speak(text, voice, engines.params("neutral", 0.5, 1.0))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Couldn't make a sample: {e}")
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, audio, engines.SAMPLE_RATE, subtype="PCM_16")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "max-age=86400"})


MAX_UPLOAD_BYTES = 4_000_000_000


async def _save_upload(file: UploadFile, folder=None):
    """Stream an upload to a temporary file, without holding it in memory."""
    suffix = re.sub(r"[^.a-zA-Z0-9]", "", Path(file.filename or "clip").suffix)[:8]
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=folder)
    size = 0
    with tmp:
        while chunk := await file.read(4 * 1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, "That file is too big (4 GB at most)")
            tmp.write(chunk)
    return Path(tmp.name)


def _voice_changed(vid):
    key = vid.split(":", 1)[1]
    engines.chatterbox.forget_voice(key)
    voices.forget_sample(vid)


@app.post("/api/voices")
async def add_voice(name: str = Form(...), gender: str = Form("unknown"), description: str = Form(""),
                    files: list[UploadFile] = File(None), file: UploadFile | None = File(None)):
    """Make a voice from one or more audio or video clips of the same person (form field files, or file)."""
    uploads = [f for f in (files or []) + ([file] if file else []) if f and f.filename]
    if not uploads:
        raise HTTPException(400, "Choose at least one audio or video clip")
    paths = [await _save_upload(f) for f in uploads]
    try:
        vid = voices.add(name, gender, description, paths)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        for p in paths:
            p.unlink(missing_ok=True)
    _voice_changed(vid)
    return {"id": vid}


@app.post("/api/voices/{key}/clips")
async def add_voice_clips(key: str, files: list[UploadFile] = File(...)):
    """Add more clips of the same person to one of your voices."""
    paths = [await _save_upload(f) for f in files if f.filename]
    try:
        vid = voices.add_clips(key, paths)
    except KeyError:
        raise HTTPException(404, "No voice with that name")
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        for p in paths:
            p.unlink(missing_ok=True)
    _voice_changed(vid)
    return {"id": vid, "clips": len(voices.clip_files(key))}


# ----- finding voices in videos and long recordings -----

@app.post("/api/speakers")
async def find_speakers(file: UploadFile = File(...), speakers: str = Form("")):
    """Upload a video or recording; the people talking in it are found in the background."""
    (config.CONFIG_DIR / "analysis").mkdir(parents=True, exist_ok=True)
    path = await _save_upload(file, folder=config.CONFIG_DIR / "analysis")
    wanted = int(speakers) if speakers.strip().isdigit() and int(speakers) > 0 else None
    return {"id": speakers_mod.start(path, file.filename or "upload", wanted)}


@app.get("/api/speakers/{aid}")
def speakers_result(aid: str):
    try:
        return speakers_mod.get(aid)
    except KeyError:
        raise HTTPException(404, "Not found, or cleared after two days")


@app.post("/api/speakers/{aid}/regroup")
def speakers_regroup(aid: str, body: dict):
    """Group the lines again, into a set number of speakers (or 0 to decide by itself)."""
    try:
        wanted = int(body.get("speakers") or 0) or None
        return speakers_mod.regroup(aid, wanted)
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


def _wav(audio):
    buf = io.BytesIO()
    sf.write(buf, audio, engines.SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return Response(buf.getvalue(), media_type="audio/wav")


@app.get("/api/speakers/{aid}/speaker/{n}")
def speakers_sample(aid: str, n: int):
    try:
        return _wav(speakers_mod.speaker_sample(aid, n))
    except KeyError:
        raise HTTPException(404, "Not found")


@app.get("/api/speakers/{aid}/line/{i}")
def speakers_line(aid: str, i: int):
    try:
        seg = next(s for s in speakers_mod.get(aid)["segments"] if s["index"] == i)
    except (KeyError, StopIteration):
        raise HTTPException(404, "Not found")
    return _wav(speakers_mod.audio_of(aid, seg["start"], seg["end"]))


class MakeVoice(BaseModel):
    lines: list[int]
    name: str = ""
    gender: str = "unknown"
    description: str = ""
    add_to: str = ""


@app.post("/api/speakers/{aid}/voice")
def speakers_make_voice(aid: str, body: MakeVoice):
    """Make a voice from the chosen lines, or add them to one of your voices (add_to)."""
    if not body.add_to and not body.name.strip():
        raise HTTPException(400, "Give the voice a name")
    if body.add_to and not voices.path(body.add_to):
        raise HTTPException(404, "No voice with that name")
    try:
        vid = speakers_mod.make_voice(aid, body.lines, body.name, body.gender, body.description,
                                      add_to=body.add_to or None)
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _voice_changed(vid)
    return {"id": vid}


@app.delete("/api/voices/{key}")
def delete_voice(key: str):
    voices.remove(key)
    engines.chatterbox.forget_voice(key)
    voices.forget_sample(f"chatterbox:{key}")
    return {"ok": True}


@app.get("/api/voices/{key}/clip")
def voice_clip(key: str):
    p = voices.path(key)
    if not p:
        raise HTTPException(404, "No clip")
    return FileResponse(p, media_type="audio/wav")


class Preview(BaseModel):
    text: str
    voice: str = "chatterbox:default"
    emotion: str = "neutral"
    intensity: float = 0.5
    check: bool = False


@app.post("/api/preview")
def preview(p: Preview):
    """Speak a line with a voice and feeling, to try voices out. Returns a WAV."""
    text = story.speakable(p.text)[:400]
    if not text:
        raise HTTPException(400, "Type something to say")
    params = engines.params(p.emotion, p.intensity, 1.0)
    try:
        if p.check:
            audio, _ = jobs.render_line(text, p.voice, params)
        else:
            audio = engines.speak(text, p.voice, params)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Couldn't render: {e}")
    buf = io.BytesIO()
    sf.write(buf, audio, engines.SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return Response(buf.getvalue(), media_type="audio/wav")


# ----- stories -----

@app.get("/api/jobs")
def list_jobs():
    return jobs.all_jobs()


@app.post("/api/jobs")
async def new_job(title: str = Form(""), author: str = Form(""), text: str = Form(""),
                  format: str = Form(""), review: str = Form(""), file: UploadFile | None = File(None)):
    """Start a story. Send the text as a .txt or .md file, or in the text field."""
    if file is not None and file.filename:
        raw = await file.read(MAX_STORY_BYTES + 1)
        if len(raw) > MAX_STORY_BYTES:
            raise HTTPException(413, "That file is too big (20 MB at most)")
        text = raw.decode("utf-8", errors="replace")
        if not title:
            title = re.sub(r"[_\-]+", " ", Path(file.filename).stem).strip()
            title = title.title() if title.islower() else title
    auto = None
    if review in ("0", "false", "no"):
        auto = True
    elif review in ("1", "true", "yes"):
        auto = False
    try:
        job = jobs.create(title.strip() or "Story", text, author=author.strip(), output_format=format or None,
                          auto_render=auto)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return job


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    return _job_or_404(job_id)


@app.get("/api/jobs/{job_id}/script")
def job_script(job_id: str):
    _job_or_404(job_id)
    return jobs.script(job_id)


@app.put("/api/jobs/{job_id}/script")
def edit_script(job_id: str, changes: dict):
    job = _job_or_404(job_id)
    if job["status"] in jobs.WORKING:
        raise HTTPException(409, "Wait until it has finished or stop it first")
    return jobs.update_script(job_id, changes)


@app.post("/api/jobs/{job_id}/render")
def render(job_id: str):
    _job_or_404(job_id)
    try:
        jobs.start_render(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return jobs.get(job_id)


@app.post("/api/jobs/{job_id}/retag")
def retag(job_id: str):
    _job_or_404(job_id)
    try:
        jobs.retag(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return jobs.get(job_id)


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id: str):
    _job_or_404(job_id)
    jobs.cancel(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete(job_id: str):
    _job_or_404(job_id)
    jobs.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/audio")
def download(job_id: str):
    _job_or_404(job_id)
    p = jobs.output_path(job_id)
    if not p:
        raise HTTPException(404, "Not rendered yet")
    media = "audio/mp4" if p.suffix == ".m4b" else "audio/mpeg"
    return FileResponse(p, media_type=media, filename=p.name)


@app.exception_handler(KeyError)
async def not_found(request, exc):
    return JSONResponse({"detail": "Not found"}, status_code=404)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    setup()
    config.ensure_dirs()
    s = config.load()
    log.info(f"Narrator {__version__} starting on port {config.WEB_PORT}")
    if not s["ollama_url"]:
        log.info("No Ollama address yet: speakers and feelings will be guessed from the text. Set it in Settings")
    if not s["kokoro_url"]:
        log.info("No Kokoro address yet: only Chatterbox voices will be used")
    jobs.recover()
    jobs.start_worker()
    uvicorn.run(app, host="0.0.0.0", port=config.WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
