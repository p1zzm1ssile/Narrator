"""The voices: Chatterbox inside this container, and Kokoro over HTTP.

A voice is named "chatterbox:<name>" or "kokoro:<name>". "chatterbox:default"
is Chatterbox's built-in voice; any other Chatterbox voice is a short clip of
someone talking, saved in /config/voices, that Chatterbox copies.
"""

import io
import threading
import time

import numpy as np
import requests
import soundfile as sf

from core import config, voices
from utils.logger import log

SAMPLE_RATE = 24000

# emotion: (exaggeration, cfg_weight, temperature, kokoro speed)
# Chatterbox acts more with higher exaggeration; a lower cfg_weight lets the
# pace follow the feeling instead of staying even.
EMOTION = {
    "neutral":   (0.50, 0.50, 0.80, 1.00),
    "happy":     (0.70, 0.45, 0.80, 1.05),
    "excited":   (0.95, 0.30, 0.85, 1.10),
    "amused":    (0.70, 0.45, 0.85, 1.05),
    "tender":    (0.40, 0.55, 0.70, 0.92),
    "sad":       (0.45, 0.55, 0.70, 0.88),
    "angry":     (1.00, 0.30, 0.85, 1.05),
    "fearful":   (0.80, 0.35, 0.85, 1.10),
    "surprised": (0.85, 0.35, 0.85, 1.05),
    "tense":     (0.70, 0.40, 0.80, 1.05),
    "serious":   (0.40, 0.55, 0.70, 0.95),
    "sarcastic": (0.65, 0.45, 0.80, 1.00),
    "whisper":   (0.30, 0.60, 0.70, 0.90),
}


def params(emotion, intensity, expressiveness):
    """How to voice a line: Chatterbox settings and Kokoro speed."""
    exag, cfg, temp, speed = EMOTION.get(emotion, EMOTION["neutral"])
    scale = max(0.0, min(1.0, float(intensity))) * 2 * max(0.0, float(expressiveness))
    neutral = EMOTION["neutral"]
    return {
        "exaggeration": round(max(0.25, min(1.3, neutral[0] + (exag - neutral[0]) * scale)), 3),
        "cfg_weight": round(max(0.2, min(0.7, neutral[1] + (cfg - neutral[1]) * scale)), 3),
        "temperature": round(neutral[2] + (temp - neutral[2]) * min(scale, 1.0), 3),
        "speed": round(1.0 + (speed - 1.0) * min(scale, 1.5), 3),
    }


class Chatterbox:
    def __init__(self):
        self.model = None
        self.device = None
        self.lock = threading.RLock()
        self.conds = {}
        self.last_used = 0.0

    def _load(self):
        if self.model is not None:
            return
        import torch
        from chatterbox.tts import ChatterboxTTS

        wanted = config.get("tts_device")
        if wanted == "auto":
            wanted = "cuda" if torch.cuda.is_available() else "cpu"
        if wanted == "cuda" and not torch.cuda.is_available():
            log.warning("TTS device is cuda but no GPU is visible to the container, using the CPU (slow)")
            wanted = "cpu"
        if wanted == "cuda":
            log.info(f"Loading Chatterbox on {torch.cuda.get_device_name(0)}")
        else:
            log.info("Loading Chatterbox on the CPU. This works, but a story takes many times longer than on a GPU")
        started = time.time()
        self.model = ChatterboxTTS.from_pretrained(device=wanted)
        self.device = wanted
        self.conds = {"default": self.model.conds}
        log.info(f"Chatterbox ready in {time.time() - started:.0f}s")

    def unload(self):
        with self.lock:
            if self.model is None:
                return
            self.model = None
            self.conds = {}
            try:
                import gc
                import torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            log.info("Chatterbox unloaded to free the GPU")

    def idle_unload(self):
        minutes = config.get("unload_after_minutes")
        if self.model is not None and minutes > 0 and time.time() - self.last_used > minutes * 60:
            self.unload()

    def forget_voice(self, name):
        with self.lock:
            self.conds.pop(name, None)

    def speak(self, text, voice, p, seed=None):
        with self.lock:
            self._load()
            import torch
            if voice not in self.conds:
                path = voices.path(voice)
                if path is None:
                    log.warning(f"Voice clip for {voice} is missing, using Chatterbox's own voice")
                    voice = "default"
                else:
                    self.model.prepare_conditionals(str(path), exaggeration=p["exaggeration"])
                    self.conds[voice] = self.model.conds
            self.model.conds = self.conds[voice]
            if seed is not None:
                torch.manual_seed(seed)
            wav = self.model.generate(
                text,
                exaggeration=p["exaggeration"],
                cfg_weight=p["cfg_weight"],
                temperature=p["temperature"],
            )
            self.last_used = time.time()
            return wav.squeeze(0).cpu().numpy().astype(np.float32)


class Kokoro:
    def voices(self):
        url = config.get("kokoro_url")
        if not url:
            return []
        r = requests.get(f"{url}/v1/audio/voices", timeout=10)
        r.raise_for_status()
        data = r.json()
        found = data.get("voices", data) if isinstance(data, dict) else data
        return sorted(v if isinstance(v, str) else v.get("id") or v.get("name") for v in found)

    def speak(self, text, voice, p):
        url = config.get("kokoro_url")
        if not url:
            raise RuntimeError("Kokoro address isn't set (Settings)")
        r = requests.post(
            f"{url}/v1/audio/speech",
            json={"model": "kokoro", "input": text, "voice": voice, "response_format": "wav",
                  "speed": p["speed"], "stream": False},
            timeout=300,
        )
        r.raise_for_status()
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=True)
        return resample(audio.mean(axis=1), sr)


def resample(audio, sr, target=SAMPLE_RATE):
    if sr == target:
        return audio.astype(np.float32)
    import librosa
    return librosa.resample(audio.astype(np.float32), orig_sr=sr, target_sr=target)


chatterbox = Chatterbox()
kokoro = Kokoro()


def speak(text, voice, p, seed=None):
    """Render text with a voice like "chatterbox:anna" or "kokoro:af_bella". Returns 24 kHz mono float32."""
    engine, _, name = voice.partition(":")
    if engine == "kokoro":
        return kokoro.speak(text, name, p)
    return chatterbox.speak(text, name or "default", p, seed=seed)
