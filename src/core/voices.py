"""The voice library in /config/voices, and choosing a voice for each character.

Each Chatterbox voice is <name>.wav (10 to 20 seconds of one person talking
clearly) with its details in voices.json.
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


def path(name):
    if not name or name == "default" or "/" in name or ".." in name:
        return None
    p = config.VOICES_DIR / f"{name}.wav"
    return p if p.exists() else None


def library():
    """Chatterbox voices: [{"id", "name", "gender", "description"}]."""
    meta = _read_meta()
    out = [{"id": "chatterbox:default", "name": "Chatterbox default", "gender": "female", "accent": "American",
            "english": True, "description": "Chatterbox's built-in voice"}]
    for f in sorted(config.VOICES_DIR.glob("*.wav")):
        m = meta.get(f.stem, {})
        out.append({"id": f"chatterbox:{f.stem}", "name": m.get("name", f.stem), "accent": "",
                    "english": True, "gender": m.get("gender", "unknown"), "description": m.get("description", "")})
    return out


def add(name, gender, description, upload_path):
    """Convert an uploaded clip to 24 kHz mono WAV, at most 25 seconds, and save it."""
    key = slug(name)
    config.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    out = config.VOICES_DIR / f"{key}.wav"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(upload_path),
         "-t", "25", "-ac", "1", "-ar", "24000", "-af", "silenceremove=start_periods=1:start_threshold=-45dB",
         str(out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not out.exists():
        raise ValueError(f"Couldn't read that audio file: {result.stderr.strip()[-200:]}")
    with _lock:
        meta = _read_meta()
        meta[key] = {"name": name.strip() or key, "gender": gender or "unknown", "description": description or ""}
        _meta_file().write_text(json.dumps(meta, indent=2))
    return f"chatterbox:{key}"


def remove(key):
    p = path(key)
    if p:
        p.unlink()
    with _lock:
        meta = _read_meta()
        meta.pop(key, None)
        _meta_file().write_text(json.dumps(meta, indent=2))


def auto_cast(characters, narrator_voice, kokoro_voices):
    """Give each character without a voice one that fits their gender and isn't taken yet.

    Your own Chatterbox voices are used first, then Kokoro voices, and when
    both run out voices are shared.
    """
    taken = {narrator_voice}
    taken.update(c["voice"] for c in characters.values() if c.get("voice"))
    lib = [v for v in library() if v["id"] != "chatterbox:default"]

    def kokoro_gender(v):
        if v.startswith(KOKORO_MALE):
            return "male"
        if v.startswith(KOKORO_FEMALE):
            return "female"
        return "unknown"

    pool = [(v["id"], v["gender"], f'{v["name"]} {v["description"]}'.lower()) for v in lib]
    pool += [(f"kokoro:{v}", kokoro_gender(v), kokoro_info(v)["accent"].lower()) for v in kokoro_voices
             if kokoro_gender(v) != "unknown" and v[:2] in ("af", "am", "bf", "bm")]

    def fit(entry, hints):
        """How well a voice matches a character's hints, like "old, British, gruff"."""
        words = set(re.findall(r"[a-z]+", hints))
        if words & BRITISH:
            words.add("british")
        score = sum(2 for w in words if len(w) > 2 and w in entry[2])
        return score

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
