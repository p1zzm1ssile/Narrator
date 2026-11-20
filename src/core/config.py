"""Settings, kept in /config/settings.json and editable in the web UI.

Environment variables only fill in a setting the first time, before it has
been saved, so the web UI always has the last word.
"""

import json
import os
import threading
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
JOBS_DIR = CONFIG_DIR / "jobs"
VOICES_DIR = CONFIG_DIR / "voices"
WEB_PORT = int(os.environ.get("WEB_PORT", "9895"))

# name: (default, environment variable that can set it on first start)
DEFAULTS = {
    # The LLM that marks who says each line and how they feel
    "ollama_url": ("", "OLLAMA_URL"),
    "ollama_model": ("qwen2.5:7b", "OLLAMA_MODEL"),
    # Kokoro-FastAPI, for the fallback and fast voices
    "kokoro_url": ("", "KOKORO_URL"),
    # Voices: "chatterbox:default", "chatterbox:<voice file>", or "kokoro:<voice>"
    "narrator_voice": ("chatterbox:default", "NARRATOR_VOICE"),
    # Chatterbox runs on the GPU when there is one. auto, cuda or cpu
    "tts_device": ("auto", "TTS_DEVICE"),
    # 0 reads the narration flat, 1 lets it act as much as the characters
    "narrator_expressiveness": (0.5, None),
    # How much the characters act, 0 to 1.5
    "character_expressiveness": (1.0, None),
    # Free the GPU after this many idle minutes, so Ollama and Kokoro can use it
    "unload_after_minutes": (5, None),
    # Whisper listens to each clip and re-renders it when the words don't match.
    # builtin (faster-whisper inside this container), external (whisper-asr-webservice) or off
    "whisper_mode": ("builtin", "WHISPER_MODE"),
    "whisper_model": ("base.en", "WHISPER_MODEL"),
    "whisper_device": ("cpu", "WHISPER_DEVICE"),
    "whisper_url": ("", "WHISPER_URL"),
    "whisper_min_match": (0.8, None),
    "max_retries": (2, None),
    # Output
    "output_format": ("m4b", "OUTPUT_FORMAT"),
    "bitrate_kbps": (64, None),
    "loudness_lufs": (-18, None),
    # Stop after the LLM step so the cast and lines can be checked before rendering
    "review_before_render": (True, None),
    # Pauses in seconds
    "pause_sentence": (0.25, None),
    "pause_segment": (0.35, None),
    "pause_speaker_change": (0.5, None),
    "pause_paragraph": (0.8, None),
    "pause_chapter": (2.0, None),
}

_lock = threading.Lock()
_settings = {}


def _coerce(value, default):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return str(value).strip()


def load():
    global _settings
    saved = {}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            saved = {}
    result = {}
    for name, (default, env) in DEFAULTS.items():
        if name in saved:
            raw = saved[name]
        elif env and os.environ.get(env):
            raw = os.environ[env]
        else:
            raw = default
        try:
            result[name] = _coerce(raw, default)
        except (TypeError, ValueError):
            result[name] = default
    with _lock:
        _settings = result
    return dict(result)


def get(name=None):
    if not _settings:
        load()
    with _lock:
        return dict(_settings) if name is None else _settings[name]


def update(changes):
    """Save the changes that name a known setting. Returns the new settings."""
    current = get()
    for name, value in (changes or {}).items():
        if name not in DEFAULTS:
            continue
        default = DEFAULTS[name][0]
        try:
            current[name] = _coerce(value, default)
        except (TypeError, ValueError):
            raise ValueError(f"{name} should be like {default!r}")
    for key in ("ollama_url", "kokoro_url", "whisper_url"):
        current[key] = current[key].rstrip("/")
    if current["output_format"] not in ("m4b", "mp3"):
        raise ValueError("output_format should be m4b or mp3")
    if current["whisper_mode"] not in ("builtin", "external", "off"):
        raise ValueError("whisper_mode should be builtin, external or off")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2))
    tmp.replace(SETTINGS_FILE)
    with _lock:
        global _settings
        _settings = current
    return dict(current)


def ensure_dirs():
    for d in (CONFIG_DIR, JOBS_DIR, VOICES_DIR):
        d.mkdir(parents=True, exist_ok=True)
