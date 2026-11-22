"""Stories being narrated: saved in /config/jobs/<id>/, worked through one at a time.

A job goes: tagging -> review (if turned on) -> rendering -> mixing -> done.
Rendered clips are kept by a hash of their text, voice and settings, so a job
that stops part way, or is rendered again after a change, only re-renders what
changed.
"""

import hashlib
import json
import queue
import re
import shutil
import threading
import time
import uuid

import soundfile as sf

from core import checker, config, engines, mixer, story, tagger, voices
from utils.logger import log

WORKING = ("queued", "tagging", "rendering", "mixing")
_lock = threading.RLock()
_tag_queue = queue.Queue()
_render_queue = queue.Queue()
_cancel = set()
_current = {"job": None, "tagging": None}
_waiting = []  # render jobs in the order they were queued


def _dir(job_id):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id or ""):
        raise KeyError(job_id)
    return config.JOBS_DIR / job_id


def _write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def get(job_id):
    path = _dir(job_id) / "job.json"
    if not path.exists():
        raise KeyError(job_id)
    with _lock:
        return json.loads(path.read_text())


def save(job):
    with _lock:
        job["updated"] = time.time()
        _write(_dir(job["id"]) / "job.json", job)


def set_state(job_id, **changes):
    with _lock:
        job = get(job_id)
        job.update(changes)
        save(job)
        return job


def script(job_id):
    return json.loads((_dir(job_id) / "script.json").read_text())


def save_script(job_id, data):
    with _lock:
        _write(_dir(job_id) / "script.json", data)


def all_jobs():
    out = []
    for p in sorted(config.JOBS_DIR.glob("*/job.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda j: -j.get("created", 0))


def create(title, text, author="", output_format=None, auto_render=None):
    text = text.strip()
    if not text:
        raise ValueError("The story is empty")
    job_id = uuid.uuid4().hex[:12]
    d = config.JOBS_DIR / job_id
    (d / "clips").mkdir(parents=True)
    (d / "story.txt").write_text(text)
    parsed = story.parse_any(text, title or "Story")
    parsed.setdefault("characters", {})
    save_script(job_id, parsed)
    s = config.get()
    job = {
        "id": job_id, "title": title or "Story", "author": author or "", "created": time.time(),
        "status": "queued", "next": "tag", "message": "Waiting to start",
        "done": 0, "total": len(parsed["segments"]),
        "format": output_format if output_format in ("m4b", "mp3") else s["output_format"],
        "review": s["review_before_render"] if auto_render is None else not auto_render,
        "output": None, "error": None, "chapters": len(parsed["chapters"]),
        "characters": 0, "seconds": 0,
    }
    save(job)
    _tag_queue.put(job_id)
    log.info(f"New story {job['title']!r}: {len(parsed['chapters'])} chapter(s), {len(parsed['segments'])} segments")
    return job


def start_render(job_id):
    job = get(job_id)
    if job["status"] in WORKING:
        raise ValueError("This story is already being worked on")
    set_state(job_id, status="queued", next="render", message="Waiting to render", error=None)
    _queue_render(job_id)


def retag(job_id):
    job = get(job_id)
    if job["status"] in WORKING:
        raise ValueError("This story is already being worked on")
    # Read the story again, so a newer version of Narrator's reader is used
    parsed = story.parse_any((_dir(job_id) / "story.txt").read_text(), job["title"])
    parsed.setdefault("characters", {})
    save_script(job_id, parsed)
    set_state(job_id, status="queued", next="tag", message="Waiting to start", error=None,
              chapters=len(parsed["chapters"]), total=len(parsed["segments"]))
    _tag_queue.put(job_id)


def cancel(job_id):
    job = get(job_id)
    if job["status"] in WORKING:
        _cancel.add(job_id)
        if job["status"] == "queued":
            with _lock:
                if job_id in _waiting:
                    _waiting.remove(job_id)
            set_state(job_id, status="stopped", message="Stopped before it started")
            _update_waiting_messages()


def delete(job_id):
    cancel(job_id)
    for _ in range(50):
        if job_id not in (_current["job"], _current["tagging"]):
            break
        time.sleep(0.2)
    shutil.rmtree(_dir(job_id), ignore_errors=True)


def update_script(job_id, changes):
    """Apply edits from the review page: character voices and genders, and per-line speaker and emotion."""
    with _lock:
        data = script(job_id)
        chars = data.setdefault("characters", {})
        for name, c in (changes.get("characters") or {}).items():
            entry = chars.setdefault(name, {})
            for key in ("voice", "gender", "description"):
                if key in c and c[key] is not None:
                    entry[key] = str(c[key])
        by_id = {s["id"]: s for s in data["segments"]}
        for item in changes.get("segments") or []:
            seg = by_id.get(item.get("id"))
            if not seg:
                continue
            if item.get("speaker"):
                seg["speaker"] = str(item["speaker"]).strip()
                if seg["speaker"] not in chars and seg["speaker"] != "Narrator":
                    chars[seg["speaker"]] = {"gender": "unknown"}
            if item.get("emotion") in tagger.EMOTIONS:
                seg["emotion"] = item["emotion"]
            if "intensity" in item:
                try:
                    seg["intensity"] = max(0.0, min(1.0, float(item["intensity"])))
                except (TypeError, ValueError):
                    pass
        if "narrator_voice" in changes and changes["narrator_voice"]:
            data["narrator_voice"] = str(changes["narrator_voice"])
        save_script(job_id, data)
        return data


# ----- the worker -----

def recover():
    """Put stories that were cut off by a restart back in the queue."""
    for job in sorted(all_jobs(), key=lambda j: j.get("created", 0)):
        if job["status"] not in WORKING:
            continue
        if job["status"] in ("rendering", "mixing"):
            job["next"] = "render"
        nxt = job.get("next", "tag")
        set_state(job["id"], status="queued", next=nxt, message="Picking up after a restart")
        log.info(f"Picking up {job['title']!r} ({job['id']}) after a restart: "
                 f"{'rendering' if nxt == 'render' else 'casting'} from where it stopped")
        if nxt == "render":
            _queue_render(job["id"])
        else:
            _tag_queue.put(job["id"])


def _queue_render(job_id):
    with _lock:
        if job_id not in _waiting:
            _waiting.append(job_id)
    _render_queue.put(job_id)
    _update_waiting_messages()


def _update_waiting_messages():
    """Tell each waiting story what it's waiting for."""
    with _lock:
        waiting = list(_waiting)
    running = _current["job"]
    title = None
    if running:
        try:
            title = get(running)["title"]
        except KeyError:
            title = None
    for n, job_id in enumerate(waiting):
        try:
            job = get(job_id)
        except KeyError:
            continue
        if job["status"] != "queued":
            continue
        if title:
            ahead = f" and {n} other stor{'y' if n == 1 else 'ies'}" if n else ""
            msg = f"Waiting for {title!r}{ahead} to finish rendering"
        else:
            msg = "Waiting to render"
        if job.get("message") != msg:
            set_state(job_id, message=msg)


def _tag_worker():
    while True:
        job_id = _tag_queue.get()
        try:
            job = get(job_id)
        except KeyError:
            continue
        if job_id in _cancel or job["status"] != "queued" or job.get("next") != "tag":
            _cancel.discard(job_id)
            continue
        _current["tagging"] = job_id
        try:
            _tag(job_id)
            job = get(job_id)
            if job["review"]:
                set_state(job_id, status="review", next="render",
                          message="Check the cast and lines, then press Render")
                log.info(f"{job['title']!r} is cast and waiting for you: check it and press Render")
            else:
                set_state(job_id, status="queued", next="render", message="Waiting to render")
                _queue_render(job_id)
        except _Cancelled:
            set_state(job_id, status="stopped", message="Stopped")
            log.info(f"Stopped casting {job_id}")
        except Exception as e:  # noqa: BLE001 - the job shows the error, the worker carries on
            log.exception(f"Casting story {job_id} failed")
            set_state(job_id, status="failed", error=str(e), message=f"Failed: {e}")
        finally:
            _cancel.discard(job_id)
            _current["tagging"] = None


def _render_worker():
    while True:
        try:
            job_id = _render_queue.get(timeout=30)
        except queue.Empty:
            try:
                engines.chatterbox.idle_unload()
            except Exception as e:  # noqa: BLE001 - never let this stop the worker
                log.warning(f"Couldn't free the GPU ({e})")
            continue
        with _lock:
            if job_id in _waiting:
                _waiting.remove(job_id)
        try:
            job = get(job_id)
        except KeyError:
            continue
        if job_id in _cancel or job["status"] != "queued" or job.get("next") != "render":
            _cancel.discard(job_id)
            continue
        _current["job"] = job_id
        _update_waiting_messages()
        started = time.time()
        try:
            _render(job_id)
        except _Cancelled:
            set_state(job_id, status="stopped", message="Stopped")
            log.info(f"Stopped rendering {job['title']!r} after {_minutes(time.time() - started)}")
        except Exception as e:  # noqa: BLE001 - the job shows the error, the worker carries on
            log.exception(f"Rendering {job['title']!r} ({job_id}) failed")
            set_state(job_id, status="failed", error=str(e), message=f"Failed: {e}")
        finally:
            _cancel.discard(job_id)
            _current["job"] = None
            _update_waiting_messages()


class _Cancelled(Exception):
    pass


def _check_cancel(job_id):
    if job_id in _cancel:
        raise _Cancelled()


def _tag(job_id):
    s = config.get()
    data = script(job_id)
    log.info(f"Casting {get(job_id)['title']!r}: {len(data['segments'])} segments"
             f"{', tagged in the story' if data.get('pretagged') else ''}")
    if data.get("pretagged"):
        # The story already says who speaks each line and how, so the LLM isn't needed
        set_state(job_id, status="tagging", message="Casting the characters from the story's tags", done=0)
        for c in data.get("characters", {}).values():
            c.pop("voice", None)
    else:
        set_state(job_id, status="tagging", message="Working out who says what", done=0)
        data["characters"] = {}
        for seg in data["segments"]:
            if seg["kind"] == "pause":
                continue
            seg["speaker"] = "Narrator" if seg["kind"] != "dialogue" else "Unknown"
            seg["emotion"], seg["intensity"] = "neutral", 0.5

        def progress(done, total):
            _check_cancel(job_id)
            set_state(job_id, done=done, total=total, message=f"Working out who says what ({done} of {total})")

        tagger.tag(data, s, progress=progress, cancelled=lambda: job_id in _cancel)
        _check_cancel(job_id)
    for c in data["characters"].values():
        c["lines"] = 0
    for seg in data["segments"]:
        if seg["speaker"] in data["characters"]:
            data["characters"][seg["speaker"]]["lines"] += 1
    data["characters"] = {n: c for n, c in data["characters"].items() if c["lines"] > 0}
    data["narrator_voice"] = s["narrator_voice"]
    try:
        kokoro_voices = engines.kokoro.voices()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Couldn't list Kokoro voices ({e}), casting from your own voices only")
        kokoro_voices = []
    voices.auto_cast(data["characters"], data["narrator_voice"], kokoro_voices)
    save_script(job_id, data)
    set_state(job_id, characters=len(data["characters"]), done=0, total=len(data["segments"]))
    cast = ", ".join(f"{n} -> {c['voice']}" for n, c in data["characters"].items())
    log.info(f"Cast {get(job_id)['title']!r}: narrator -> {data['narrator_voice']}" + (f", {cast}" if cast else ""))


def voice_for(seg, data):
    if seg["kind"] == "dialogue":
        c = data.get("characters", {}).get(seg["speaker"], {})
        if c.get("voice"):
            return c["voice"]
    return data.get("narrator_voice") or config.get("narrator_voice")


def _clip_key(text, voice, p):
    raw = json.dumps([text, voice, p], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def render_line(text, voice, p, check=True):
    """Render one piece, checking it with Whisper and re-rendering up to max_retries times.

    Returns (audio, score). A Chatterbox voice that keeps failing falls back to
    Kokoro's matching voice when Kokoro is set up.
    """
    s = config.get()
    tries = 1 + (max(0, s["max_retries"]) if check else 0)
    best, best_score = None, -1.0
    for attempt in range(tries):
        audio = engines.speak(text, voice, p, seed=attempt * 7919 + 1)
        seconds = len(audio) / engines.SAMPLE_RATE
        score = 1.0 if mixer.trim(audio).size else 0.0
        if not checker.plausible_length(text, seconds):
            score = min(score, 0.3)
        if check and checker.enabled() and score > 0.3:
            try:
                heard = checker.transcribe(audio, engines.SAMPLE_RATE)
                score = checker.match(text, heard)
                if score < s["whisper_min_match"]:
                    log.info(f"Re-rendering (match {score:.0%}): {text[:60]!r} heard as {heard[:60]!r}")
            except Exception as e:  # noqa: BLE001
                log.warning(f"Whisper check skipped ({e})")
        if score > best_score:
            best, best_score = audio, score
        if best_score >= s["whisper_min_match"]:
            break
    if best_score < 0.5 and voice.startswith("chatterbox") and config.get("kokoro_url"):
        log.info(f"Chatterbox kept failing on {text[:60]!r}, using Kokoro for this line")
        try:
            best = engines.speak(text, "kokoro:" + _kokoro_stand_in(voice), p)
            best_score = 0.5
        except Exception as e:  # noqa: BLE001
            log.warning(f"Kokoro fallback failed too ({e})")
    return best, best_score


def _kokoro_stand_in(voice):
    meta = {v["id"]: v for v in voices.library()}
    gender = meta.get(voice, {}).get("gender", "female")
    return "am_michael" if gender == "male" else "af_heart"


def _render(job_id):
    s = config.get()
    job = set_state(job_id, status="rendering", message="Rendering voices", done=0, error=None)
    data = script(job_id)
    clips_dir = _dir(job_id) / "clips"
    clips_dir.mkdir(exist_ok=True)
    clip_paths, used, low = {}, set(), 0

    # Work out every clip first, so progress counts clips, not segments
    plan = []
    for seg in data["segments"]:
        clip_paths[seg["id"]] = []
        if seg["kind"] == "pause":
            continue
        voice = voice_for(seg, data)
        expressive = s["character_expressiveness"] if seg["kind"] == "dialogue" else s["narrator_expressiveness"]
        p = engines.params(seg["emotion"], seg["intensity"], expressive)
        for piece in story.chunks(seg["text"]):
            path = clips_dir / f"{_clip_key(piece, voice, p)}.wav"
            plan.append((seg, piece, voice, p, path))
            clip_paths[seg["id"]].append(path)
            used.add(path.name)
    todo = [c for c in plan if not c[4].exists()]
    total = len(plan)
    voices_used = sorted({c[2] for c in todo})
    log.info(f"Rendering {job['title']!r}: {total} lines, {len(todo)} to render"
             f"{f' ({total - len(todo)} kept from before)' if len(todo) < total else ''}, "
             f"voices: {', '.join(voices_used) or 'none needed'}")
    set_state(job_id, done=total - len(todo), total=total,
              message=f"Rendering voices ({total - len(todo)} of {total})")

    started = last_log = time.time()
    for n, (seg, piece, voice, p, path) in enumerate(todo, start=1):
        _check_cancel(job_id)
        t0 = time.time()
        try:
            audio, score = render_line(piece, voice, p)
        except Exception as e:
            raise RuntimeError(f"line {seg['id']} ({seg['speaker']}, {voice}): {e}") from e
        took = time.time() - t0
        if took > 90:
            log.warning(f"One line took {took:.0f}s to render ({voice}): {piece[:60]!r}")
        log.debug(f"Rendered line {seg['id']} as {seg['speaker']} ({voice}, {seg['emotion']}) "
                  f"in {took:.1f}s, match {score:.0%}")
        if score < s["whisper_min_match"]:
            low += 1
        sf.write(path, audio, engines.SAMPLE_RATE, subtype="PCM_16")
        done = total - len(todo) + n
        elapsed = time.time() - started
        left = elapsed / n * (len(todo) - n)
        msg = f"Rendering voices ({done} of {total}, {done * 100 // total}%, about {_minutes(left)} left)"
        set_state(job_id, done=done, total=total, message=msg)
        if time.time() - last_log >= 30 or n == len(todo):
            log.info(f"{job['title']!r}: {msg[len('Rendering voices ('):-1]}")
            last_log = time.time()

    # Clips from earlier renders that are no longer needed
    for f in clips_dir.glob("*.wav"):
        if f.name not in used:
            f.unlink(missing_ok=True)

    _check_cancel(job_id)
    set_state(job_id, status="mixing", message="Joining the clips and making the audiobook")
    log.info(f"{job['title']!r}: joining {total} lines into an {job['format'].upper()}")
    work = _dir(job_id) / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    chapters = mixer.build_chapters(data, clip_paths, s, work)
    if not chapters:
        raise RuntimeError("Nothing was rendered")
    fmt = job["format"]
    out = _dir(job_id) / f"{_safe_name(job['title'])}.{fmt}"
    for old in _dir(job_id).glob("*.m4b"):
        old.unlink()
    for old in _dir(job_id).glob("*.mp3"):
        old.unlink()
    mixer.encode(chapters, out, job["title"], {**s, "output_format": fmt}, author=job.get("author", ""))
    shutil.rmtree(work, ignore_errors=True)
    seconds = sum(c[2] for c in chapters)
    note = f", {low} line(s) still didn't match well, listen for those" if low else ""
    set_state(job_id, status="done", output=out.name, seconds=round(seconds),
              message=f"Done: {_minutes(seconds)} of audio{note}")
    log.info(f"Finished {job['title']!r}: {out.name}, {_minutes(seconds)} of audio, "
             f"rendered in {_minutes(time.time() - started)}{note}")


def output_path(job_id):
    job = get(job_id)
    if not job.get("output"):
        return None
    p = _dir(job_id) / job["output"]
    return p if p.exists() else None


def _safe_name(title):
    return re.sub(r"[^\w\- ]+", "", title).strip()[:80] or "story"


def _minutes(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m}m" if h else f"{m} min"


_started = []


def start_worker():
    """Start the casting and rendering threads, once."""
    if _started:
        return
    _started.append(True)
    threading.Thread(target=_tag_worker, name="narrator-cast", daemon=True).start()
    threading.Thread(target=_render_worker, name="narrator-render", daemon=True).start()
