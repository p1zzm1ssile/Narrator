"""Find the different people talking in a video or long recording.

1. ffmpeg takes the sound out of the file.
2. Whisper (built in, on the CPU) finds each stretch of speech and what's said.
3. Chatterbox's own voice encoder turns each stretch into a voice fingerprint.
4. Stretches with similar fingerprints are grouped into speakers.

You then listen to each speaker, untick any lines that aren't them, and make
a voice from the rest. Nothing leaves the container.
"""

import json
import re
import shutil
import threading
import time
import uuid

import numpy as np
import soundfile as sf

from core import config, voices
from utils.logger import log

MAX_MINUTES = 30
MIN_EMBED_SECONDS = 1.2
SAME_SPEAKER = 0.72  # cosine similarity above which two groups are one person
_lock = threading.Lock()
_ve = {"model": None}


def _root():
    return config.CONFIG_DIR / "analysis"


def _dir(aid):
    if not re.fullmatch(r"[a-f0-9]{12}", aid or ""):
        raise KeyError(aid)
    return _root() / aid


def _write(aid, data):
    p = _dir(aid) / "result.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(p)


def get(aid):
    p = _dir(aid) / "result.json"
    if not p.exists():
        raise KeyError(aid)
    return json.loads(p.read_text())


def _update(aid, **changes):
    with _lock:
        data = get(aid)
        data.update(changes)
        _write(aid, data)
        return data


def cleanup(days=2):
    """Remove analyses older than a couple of days; they hold a copy of the uploaded file's sound."""
    if not _root().exists():
        return
    for d in _root().iterdir():
        try:
            if time.time() - d.stat().st_mtime > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def start(upload_path, filename, speakers=None):
    """Start finding speakers in an uploaded file. Returns the analysis id straight away."""
    cleanup()
    aid = uuid.uuid4().hex[:12]
    d = _root() / aid
    d.mkdir(parents=True)
    src = d / ("source" + (re.sub(r"[^.a-zA-Z0-9]", "", filename[filename.rfind("."):])[:8] if "." in filename else ""))
    shutil.move(str(upload_path), src)
    _write(aid, {"id": aid, "file": filename, "status": "working", "message": "Taking the sound out of the file",
                 "progress": 0, "speakers": [], "segments": [], "wanted": speakers, "created": time.time()})
    threading.Thread(target=_run, args=(aid, src, speakers), name=f"speakers-{aid}", daemon=True).start()
    return aid


def _run(aid, src, wanted):
    d = _dir(aid)
    try:
        # 24 kHz for making the voice, 16 kHz for Whisper and the voice encoder
        voices.convert(src, d / "audio24.wav", seconds=MAX_MINUTES * 60)
        audio24, _ = sf.read(d / "audio24.wav", dtype="float32")
        from core.engines import resample
        audio16 = resample(audio24, 24000, 16000)
        src.unlink(missing_ok=True)  # the sound is all that's needed now
        minutes = len(audio24) / 24000 / 60
        log.info(f"Finding speakers in {get(aid)['file']!r} ({minutes:.1f} min of sound)")
        _update(aid, message=f"Listening for speech ({minutes:.0f} min of sound, about {max(1, round(minutes / 3))} min)",
                progress=0.1, seconds=round(len(audio24) / 24000))

        segments = _speech(aid, audio16)
        if not segments:
            raise ValueError("No speech was found in that file")
        _update(aid, message=f"Comparing voices in {len(segments)} lines", progress=0.8)
        embeds = _embed(audio16, segments)
        np.save(d / "embeds.npy", embeds)
        labels = group(embeds, segments, wanted)
        result = _summarise(segments, embeds, labels)
        _update(aid, status="done", progress=1, message=f"Found {len(result['speakers'])} speaker(s)", **result)
        log.info(f"Found {len(result['speakers'])} speaker(s) in {get(aid)['file']!r}")
    except Exception as e:  # noqa: BLE001 - shown in the web UI
        log.exception(f"Finding speakers failed ({aid})")
        _update(aid, status="failed", message=f"Failed: {e}")


def _speech(aid, audio16):
    """Stretches of speech with their words, from Whisper with its speech detector on."""
    model = _whisper()
    english_only = config.get("whisper_model").endswith(".en")
    total = len(audio16) / 16000
    found = []
    with _whisper_lock:
        segs, _ = model.transcribe(audio16, language="en" if english_only else None, beam_size=1,
                                   vad_filter=True, condition_on_previous_text=False)
        for s in segs:
            text = s.text.strip()
            if s.end - s.start >= 0.6 and re.search(r"[A-Za-z]", text):
                found.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
            _update(aid, progress=round(0.1 + 0.7 * min(1.0, s.end / max(total, 1)), 3),
                    message=f"Listening for speech ({_clock(s.end)} of {_clock(total)})")
    for i, s in enumerate(found):
        s["index"] = i
    return found


_whisper_lock = threading.Lock()
_whisper_model = {"key": None, "model": None}


def _whisper():
    """Its own Whisper on the CPU, so a long video doesn't hold up the checks while rendering."""
    from faster_whisper import WhisperModel
    name = config.get("whisper_model")
    with _whisper_lock:
        if _whisper_model["key"] != name:
            log.info(f"Loading Whisper {name} on cpu for finding speakers")
            _whisper_model["model"] = WhisperModel(name, device="cpu", compute_type="int8",
                                                   download_root=str(config.CONFIG_DIR / "models" / "whisper"))
            _whisper_model["key"] = name
        return _whisper_model["model"]


def _voice_encoder():
    if _ve["model"] is None:
        # Its own small copy on the CPU, so it never gets in the way of rendering
        from chatterbox.models.voice_encoder import VoiceEncoder
        from huggingface_hub import hf_hub_download
        from safetensors.torch import load_file
        ve = VoiceEncoder()
        ve.load_state_dict(load_file(hf_hub_download(repo_id="ResembleAI/chatterbox", filename="ve.safetensors")))
        ve.to("cpu").eval()
        _ve["model"] = ve
    return _ve["model"]


def embed_wavs(wavs, sr):
    """Voice fingerprints (L2-normalised, one per clip)."""
    return np.asarray(_voice_encoder().embeds_from_wavs(wavs, sample_rate=sr), dtype=np.float32)


def _embed(audio16, segments):
    out = np.zeros((len(segments), 256), dtype=np.float32)
    long_enough = [s for s in segments if s["end"] - s["start"] >= MIN_EMBED_SECONDS]
    if not long_enough:
        long_enough = segments
    wavs = [audio16[int(s["start"] * 16000):int(s["end"] * 16000)] for s in long_enough]
    vecs = embed_wavs(wavs, 16000)
    for s, v in zip(long_enough, vecs):
        out[s["index"]] = v
    return out


def group(embeds, segments, wanted=None):
    """Group lines by voice. Average-linkage clustering on cosine similarity.

    With `wanted` it makes exactly that many groups; otherwise it stops when the
    two closest groups are less alike than SAME_SPEAKER.
    Short lines (no fingerprint) join the group whose lines are nearest in time.
    """
    have = [i for i in range(len(segments)) if np.linalg.norm(embeds[i]) > 0]
    labels = [-1] * len(segments)
    if not have:
        return [0] * len(segments)
    clusters = [[i] for i in have]
    cents = [embeds[i].copy() for i in have]

    def norm(v):
        n = np.linalg.norm(v)
        return v / n if n else v

    while len(clusters) > 1:
        m = np.array([norm(c) for c in cents])
        sim = m @ m.T
        np.fill_diagonal(sim, -2)
        a, b = np.unravel_index(np.argmax(sim), sim.shape)
        if wanted:
            if len(clusters) <= wanted:
                break
        elif sim[a, b] < SAME_SPEAKER:
            break
        a, b = min(a, b), max(a, b)
        clusters[a] += clusters[b]
        cents[a] = embeds[clusters[a]].mean(axis=0)
        del clusters[b], cents[b]
    for n, c in enumerate(clusters):
        for i in c:
            labels[i] = n
    for i, lab in enumerate(labels):
        if lab == -1:
            nearest = min(have, key=lambda j: abs(segments[j]["start"] - segments[i]["start"]))
            labels[i] = labels[nearest]
    return labels


def _summarise(segments, embeds, labels):
    speakers = {}
    for s, lab in zip(segments, labels):
        s["speaker"] = int(lab)
        speakers.setdefault(lab, []).append(s)
    out = []
    for lab, segs in speakers.items():
        vecs = np.array([embeds[s["index"]] for s in segs if np.linalg.norm(embeds[s["index"]]) > 0])
        cent = vecs.mean(axis=0) if len(vecs) else None
        for s in segs:
            v = embeds[s["index"]]
            s["likeness"] = round(float(v @ cent / (np.linalg.norm(cent) or 1)), 3) if cent is not None and np.linalg.norm(v) > 0 else 0.0
        out.append({"id": int(lab), "seconds": round(sum(s["end"] - s["start"] for s in segs), 1),
                    "lines": len(segs), "best": [s["index"] for s in best_lines(segs)]})
    out.sort(key=lambda sp: -sp["seconds"])
    # Number speakers by how much they talk: Speaker 1 talks most
    renumber = {sp["id"]: n for n, sp in enumerate(out)}
    for sp in out:
        sp["id"] = renumber[sp["id"]]
        sp["name"] = f"Speaker {sp['id'] + 1}"
    for s in segments:
        s["speaker"] = renumber[s["speaker"]]
    return {"speakers": out, "segments": segments}


def best_lines(segs, seconds=30):
    """The lines most like the speaker, preferring longer ones, up to `seconds` in all."""
    ranked = sorted(segs, key=lambda s: -(s.get("likeness", 0) * min(s["end"] - s["start"], 10)))
    chosen, total = [], 0.0
    for s in ranked:
        if s["end"] - s["start"] < 1.0:
            continue
        chosen.append(s)
        total += s["end"] - s["start"]
        if total >= seconds:
            break
    return sorted(chosen, key=lambda s: s["start"])


def regroup(aid, wanted):
    data = get(aid)
    if data["status"] != "done":
        raise ValueError("Still working on it")
    embeds = np.load(_dir(aid) / "embeds.npy")
    segments = data["segments"]
    labels = group(embeds, segments, wanted)
    result = _summarise(segments, embeds, labels)
    return _update(aid, wanted=wanted, message=f"Found {len(result['speakers'])} speaker(s)", **result)


def audio_of(aid, start, end):
    audio, _ = sf.read(_dir(aid) / "audio24.wav", dtype="float32", start=int(start * 24000), stop=int(end * 24000))
    return audio


def speaker_sample(aid, speaker, seconds=12):
    """A few of a speaker's best lines joined together, to hear who they are."""
    data = get(aid)
    segs = [s for s in data["segments"] if s["speaker"] == speaker]
    if not segs:
        raise KeyError(speaker)
    gap = np.zeros(int(24000 * 0.35), dtype=np.float32)
    parts = []
    for s in best_lines(segs, seconds):
        parts += [audio_of(aid, s["start"], s["end"]), gap]
    return np.concatenate(parts) if parts else audio_of(aid, segs[0]["start"], segs[0]["end"])


def make_voice(aid, indexes, name, gender, description, add_to=None):
    """Make a voice (or add to one) from the chosen lines."""
    data = get(aid)
    by_index = {s["index"]: s for s in data["segments"]}
    chosen = [by_index[i] for i in indexes if i in by_index]
    if not chosen:
        raise ValueError("Pick at least one line")
    if sum(s["end"] - s["start"] for s in chosen) < 4:
        raise ValueError("Pick at least 4 seconds of speech; 15 to 40 seconds works best")
    key = add_to or voices.slug(name)
    voices._migrate(key)
    # Lines next to each other become one clip, so the voice keeps its natural flow
    chosen.sort(key=lambda s: s["start"])
    runs, run = [], [chosen[0]]
    for s in chosen[1:]:
        if s["start"] - run[-1]["end"] < 0.8 and s["end"] - run[0]["start"] < voices.MAX_CLIP_SECONDS:
            run.append(s)
        else:
            runs.append(run)
            run = [s]
    runs.append(run)
    for run in runs:
        voices.add_clip_audio(key, audio_of(aid, run[0]["start"], run[-1]["end"]))
    voices.rebuild(key)
    if add_to:
        voices._save_meta(key, None, None, None)
    else:
        voices._save_meta(key, name, gender, description)
    return f"chatterbox:{key}"


def _clock(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"
