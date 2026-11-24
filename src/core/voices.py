"""The voice library in /config/voices, and choosing a voice for each character.

Each Chatterbox voice is <name>.wav (10 to 20 seconds of one person talking
clearly) with its details in voices.json.

Every English Kokoro voice also has an expressive copy, "chatterbox:k-<voice>":
Kokoro reads a short passage once, and Chatterbox copies that voice and acts
with it. So characters can each sound different and still show feeling,
without you having to find voice clips.
"""

import json
import re
import subprocess
import threading

from core import config

_lock = threading.Lock()
KOKORO_MALE = ("am_", "bm_")
BRITISH = {"british", "english", "scottish", "irish", "welsh", "cornish", "london", "cockney", "yorkshire", "uk"}
KOKORO_FEMALE = ("af_", "bf_")


KOKORO_LANG = {"a": "American", "b": "British", "e": "Spanish", "f": "French", "h": "Hindi",
               "i": "Italian", "j": "Japanese", "p": "Brazilian Portuguese", "z": "Mandarin"}
SAMPLE_TEXT = ("Hello, I'm {name}. The storm had been building all afternoon, and by the time the sun went down, "
               "the sea was throwing itself at the rocks. Shall we begin?")


def kokoro_info(voice):
    """Describe a Kokoro voice from its name, like af_bella: American, female, Bella."""
    prefix, _, rest = voice.partition("_")
    lang = KOKORO_LANG.get(prefix[:1], "Other") if len(prefix) == 2 else "Mix"
    gender = {"f": "female", "m": "male"}.get(prefix[1:2], "unknown") if len(prefix) == 2 else "unknown"
    name = rest.replace("_", " ").title() if rest else voice
    return {"id": f"kokoro:{voice}", "name": name, "gender": gender, "accent": lang,
            "english": lang in ("American", "British"), "description": f"{lang}, {gender}"}


def sample_path(voice_id):
    engine, _, name = voice_id.partition(":")
    safe = re.sub(r"[^a-zA-Z0-9_+\-]", "_", name)[:80]
    return config.CONFIG_DIR / "samples" / f"{engine}_{safe}.wav"


def forget_sample(voice_id):
    sample_path(voice_id).unlink(missing_ok=True)


def _meta_file():
    return config.VOICES_DIR / "voices.json"


def _read_meta():
    try:
        return json.loads(_meta_file().read_text())
    except (OSError, ValueError):
        return {}


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "voice"


COPY_PREFIX = "k-"
# Varied, natural sentences give Chatterbox the most to copy from
REFERENCE_TEXT = ("I wasn't sure I'd make it back before dark, but here I am. Honestly, the road was longer "
                  "than I remembered, and colder too. Still, there's something about this old house. "
                  "Do you hear that? It's only the wind. Come on, let's get inside and I'll tell you everything.")


def is_copy(name):
    return name.startswith(COPY_PREFIX) and re.fullmatch(r"[a-z]{2}_[a-z0-9_]+", name[len(COPY_PREFIX):]) is not None


def path(name):
    if not name or name == "default" or "/" in name or ".." in name:
        return None
    p = config.VOICES_DIR / f"{name}.wav"
    if not p.exists() and is_copy(name):
        _make_copy(name[len(COPY_PREFIX):], p)
    return p if p.exists() else None


def _make_copy(kokoro_voice, out):
    """Have Kokoro read the reference passage, to give Chatterbox a voice to copy."""
    import soundfile as sf
    from core import engines
    from utils.logger import log

    audio = engines.kokoro.speak(REFERENCE_TEXT, kokoro_voice, {"speed": 1.0})
    config.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.wav")
    sf.write(tmp, audio, engines.SAMPLE_RATE, subtype="PCM_16")
    tmp.replace(out)
    log.info(f"Made an expressive copy of Kokoro's {kokoro_voice} for Chatterbox")


def copy_info(kokoro_voice):
    info = kokoro_info(kokoro_voice)
    return {**info, "id": f"chatterbox:{COPY_PREFIX}{kokoro_voice}", "kokoro": kokoro_voice,
            "description": f"Kokoro's {info['name']} voice, acted by Chatterbox"}


def library(kokoro_voices=None):
    """Chatterbox voices: the built-in one, your own clips, and copies of Kokoro's English voices."""
    meta = _read_meta()
    out = [{"id": "chatterbox:default", "name": "Chatterbox default", "gender": "female", "accent": "American",
            "english": True, "description": "Chatterbox's built-in voice"}]
    copies = set()
    for v in kokoro_voices or []:
        if v[:2] in ("af", "am", "bf", "bm") and "+" not in v:
            out.append(copy_info(v))
            copies.add(v)
    for f in sorted(config.VOICES_DIR.glob("*.wav")):
        if f.name.endswith(".part.wav"):
            continue
        if is_copy(f.stem):
            if f.stem[len(COPY_PREFIX):] not in copies:
                out.append(copy_info(f.stem[len(COPY_PREFIX):]))
            continue
        m = meta.get(f.stem, {})
        out.append({"id": f"chatterbox:{f.stem}", "name": m.get("name", f.stem), "accent": "",
                    "english": True, "gender": m.get("gender", "unknown"), "description": m.get("description", ""),
                    "clips": max(1, len(clip_files(f.stem)))})
    return out


MAX_CLIP_SECONDS = 60
MAX_REFERENCE_SECONDS = 40


def clip_dir(key):
    return config.VOICES_DIR / key


def clip_files(key):
    """The clips a voice was made from, oldest first. Voices from before 0.1.2 have none."""
    d = clip_dir(key)
    return sorted(d.glob("clip-*.wav")) if d.is_dir() else []


def convert(upload_path, out, start=None, seconds=MAX_CLIP_SECONDS):
    """Any audio or video file (or part of one) to a 24 kHz mono WAV."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(upload_path), "-vn", "-t", f"{seconds:.3f}", "-ac", "1", "-ar", "24000", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        out.unlink(missing_ok=True)
        raise ValueError(f"Couldn't read audio from that file: {result.stderr.strip()[-200:] or 'no sound in it'}")
    return out


def _next_clip(key):
    d = clip_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    n = max([int(f.stem.split("-")[1]) for f in d.glob("clip-*.wav")] + [0]) + 1
    return d / f"clip-{n:03d}.wav"


def add_clip_audio(key, audio):
    """Save audio (24 kHz mono float) as another clip of a voice."""
    import soundfile as sf
    out = _next_clip(key)
    sf.write(out, audio, 24000, subtype="PCM_16")
    return out


def rebuild(key):
    """Join a voice's clips into the one reference Chatterbox reads.

    Each clip is trimmed and levelled, the longest goes first (Chatterbox
    takes the voice's sound from the first 10 seconds), and the rest follow,
    up to 40 seconds. Its speaker likeness is taken from every clip.
    """
    import numpy as np
    import soundfile as sf
    from core.mixer import trim

    clips = clip_files(key)
    if not clips:
        return
    parts = []
    for f in clips:
        audio, _ = sf.read(f, dtype="float32")
        audio = trim(audio.mean(axis=1) if audio.ndim > 1 else audio)
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0
        if rms > 1e-4:
            audio = np.clip(audio * (0.1 / rms), -0.99, 0.99)  # about -20 dBFS
            parts.append(audio)
    if not parts:
        raise ValueError("Those clips have no sound in them")
    parts.sort(key=len, reverse=True)
    gap = np.zeros(int(24000 * 0.3), dtype=np.float32)
    joined, total = [], 0
    for a in parts:
        if total >= MAX_REFERENCE_SECONDS * 24000:
            break
        joined += [a, gap]
        total += len(a) + len(gap)
    out = config.VOICES_DIR / f"{key}.wav"
    tmp = config.VOICES_DIR / f"{key}.part.wav"
    sf.write(tmp, np.concatenate(joined)[: MAX_REFERENCE_SECONDS * 24000], 24000, subtype="PCM_16")
    tmp.replace(out)


def _save_meta(key, name, gender, description):
    with _lock:
        meta = _read_meta()
        entry = meta.setdefault(key, {})
        entry.update({"name": (name or "").strip() or entry.get("name") or key,
                      "gender": gender or entry.get("gender") or "unknown",
                      "description": description if description is not None else entry.get("description", "")})
        _meta_file().write_text(json.dumps(meta, indent=2))


def add(name, gender, description, upload_paths, key=None):
    """Make a voice from one or more audio or video files (or add them to voice `key`). Returns its id."""
    if isinstance(upload_paths, (str, bytes)) or hasattr(upload_paths, "__fspath__"):
        upload_paths = [upload_paths]
    key = key or slug(name)
    config.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    _migrate(key)
    made = []
    try:
        for upload in upload_paths:
            made.append(convert(upload, _next_clip(key)))
        rebuild(key)
    except ValueError:
        for f in made:
            f.unlink(missing_ok=True)
        raise
    _save_meta(key, name, gender, description)
    return f"chatterbox:{key}"


def add_clips(key, upload_paths):
    """Add more clips to an existing voice."""
    if key not in _read_meta() and not (config.VOICES_DIR / f"{key}.wav").exists():
        raise KeyError(key)
    return add(_read_meta().get(key, {}).get("name", key), None, None, upload_paths, key=key)


def _migrate(key):
    """A voice made before clips were kept: its one file becomes its first clip."""
    old = config.VOICES_DIR / f"{key}.wav"
    if old.exists() and not clip_files(key):
        import shutil
        clip_dir(key).mkdir(parents=True, exist_ok=True)
        shutil.copy(old, clip_dir(key) / "clip-001.wav")


def remove(key):
    import shutil
    if not key or "/" in key or ".." in key:
        return
    (config.VOICES_DIR / f"{key}.wav").unlink(missing_ok=True)
    shutil.rmtree(clip_dir(key), ignore_errors=True)
    with _lock:
        meta = _read_meta()
        meta.pop(key, None)
        _meta_file().write_text(json.dumps(meta, indent=2))


def auto_cast(characters, narrator_voice, kokoro_voices):
    """Give each character without a voice an expressive one that fits and isn't taken yet.

    Your own Chatterbox voices come first, then Chatterbox copies of Kokoro's
    English voices. Plain Kokoro voices are never picked, as they can't act;
    choose one by hand on the story's page if you want it. When the voices
    run out they are shared.
    """
    taken = {narrator_voice}
    taken.update(c["voice"] for c in characters.values() if c.get("voice"))
    lib = library(kokoro_voices)
    own = [v for v in lib if v["id"] != "chatterbox:default" and not v.get("kokoro")]
    copies = [v for v in lib if v.get("kokoro")]
    pool = [(v["id"], v["gender"], f'{v["name"]} {v["description"]}'.lower()) for v in own]
    pool += [(v["id"], v["gender"], v["accent"].lower()) for v in copies]
    pool.append(("chatterbox:default", "female", "american"))

    def fit(entry, hints):
        """How well a voice matches a character's hints, like "old, British, gruff"."""
        words = set(re.findall(r"[a-z]+", hints))
        if words & BRITISH:
            words.add("british")
        return sum(2 for w in words if len(w) > 2 and w in entry[2])

    # The busiest speakers get first pick
    for name, c in sorted(characters.items(), key=lambda kv: -kv[1].get("lines", 0)):
        if c.get("voice"):
            continue
        gender = c.get("gender", "unknown")
        hints = f'{c.get("age", "")} {c.get("description", "")}'.lower()
        fits = [e for e in pool if gender == "unknown" or e[1] in (gender, "unknown")]
        free = [e for e in fits if e[0] not in taken]
        if free:
            choice = max(free, key=lambda e: fit(e, hints))[0]
        elif fits:
            choice = fits[len(taken) % len(fits)][0]
        else:
            choice = "chatterbox:default"
        c["voice"] = choice
        taken.add(choice)
    return characters
