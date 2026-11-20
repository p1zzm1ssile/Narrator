"""Listen back to each clip with Whisper and score how well it matches the text.

Expressive voices sometimes mumble, skip or repeat words. A clip that scores
below the setting's minimum match is rendered again, and the best try is kept.
"""

import difflib
import io
import re
import threading

import numpy as np
import requests
import soundfile as sf

from core import config
from utils.logger import log

_model = None
_model_key = None
_lock = threading.Lock()

NUMBERS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
           "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten"}


def words(text):
    text = text.lower().replace("’", "'")
    found = re.findall(r"[a-z0-9']+", text)
    return [NUMBERS.get(w, w).strip("'") for w in found if w.strip("'")]


def match(expected, heard):
    a, b = words(expected), words(heard)
    if not a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def plausible_length(text, seconds):
    """Catch runaway or cut-off clips without Whisper: normal speech is 8 to 25 characters a second."""
    chars = len(text)
    return chars / 30 - 0.5 <= seconds <= chars / 6 + 2.5


def enabled():
    return config.get("whisper_mode") != "off"


def transcribe(audio, sr):
    mode = config.get("whisper_mode")
    if mode == "external":
        return _external(audio, sr)
    if mode == "builtin":
        return _builtin(audio, sr)
    return None


def _builtin(audio, sr):
    global _model, _model_key
    from core.engines import resample
    key = (config.get("whisper_model"), config.get("whisper_device"))
    with _lock:
        if _model is None or _model_key != key:
            from faster_whisper import WhisperModel
            name, device = key
            compute = "float16" if device == "cuda" else "int8"
            log.info(f"Loading Whisper {name} on {device}")
            _model = WhisperModel(name, device=device, compute_type=compute,
                                  download_root=str(config.CONFIG_DIR / "models" / "whisper"))
            _model_key = key
        audio16 = resample(audio, sr, 16000)
        segments, _ = _model.transcribe(audio16, language="en", beam_size=1, vad_filter=False,
                                        condition_on_previous_text=False)
        return " ".join(s.text for s in segments).strip()


def _external(audio, sr):
    url = config.get("whisper_url")
    if not url:
        raise RuntimeError("Whisper address isn't set (Settings)")
    buf = io.BytesIO()
    sf.write(buf, np.asarray(audio, dtype=np.float32), sr, format="WAV", subtype="PCM_16")
    r = requests.post(f"{url}/asr", params={"task": "transcribe", "language": "en", "output": "json"},
                      files={"audio_file": ("clip.wav", buf.getvalue(), "audio/wav")}, timeout=120)
    r.raise_for_status()
    try:
        return r.json().get("text", "").strip()
    except ValueError:
        return r.text.strip()


def unload():
    global _model, _model_key
    with _lock:
        _model, _model_key = None, None
