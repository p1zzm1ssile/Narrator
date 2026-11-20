"""Join the clips with pauses into chapters, then encode one MP3 or M4B with chapter marks."""

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from core.engines import SAMPLE_RATE


def trim(audio, threshold_db=-45, keep=0.04):
    """Cut silence from both ends so the pauses between clips are even."""
    if audio.size == 0:
        return audio
    threshold = 10 ** (threshold_db / 20)
    frame = int(SAMPLE_RATE * 0.01)
    n = len(audio) // frame
    if n == 0:
        return audio
    loud = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1)) > threshold
    idx = np.nonzero(loud)[0]
    if idx.size == 0:
        return audio[:0]
    pad = int(SAMPLE_RATE * keep)
    start = max(0, idx[0] * frame - pad)
    end = min(len(audio), (idx[-1] + 1) * frame + pad)
    return audio[start:end]


def fade(audio, ms=8):
    n = min(len(audio) // 2, int(SAMPLE_RATE * ms / 1000))
    if n > 0:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        audio = audio.copy()
        audio[:n] *= ramp
        audio[-n:] *= ramp[::-1]
    return audio


def silence(seconds):
    return np.zeros(int(SAMPLE_RATE * max(0.0, seconds)), dtype=np.float32)


def pause_between(prev, seg, s):
    if prev is None:
        return 0.0
    if prev["chapter"] != seg["chapter"]:
        return s["pause_chapter"]
    if prev["kind"] == "heading" or seg["kind"] == "heading":
        return s["pause_paragraph"] * 1.5
    if prev["paragraph"] != seg["paragraph"]:
        return s["pause_paragraph"]
    if prev["speaker"] != seg["speaker"]:
        return s["pause_speaker_change"]
    return s["pause_segment"]


def build_chapters(script, clip_paths, s, out_dir):
    """clip_paths: {segment id: [wav paths]}. Writes chapter_NNN.wav files, returns [(title, path, seconds)]."""
    out_dir = Path(out_dir)
    chapters = []
    by_chapter = {}
    for seg in script["segments"]:
        by_chapter.setdefault(seg["chapter"], []).append(seg)
    for c, chapter in enumerate(script["chapters"]):
        parts, prev = [], None
        for seg in by_chapter.get(c, []):
            if seg["kind"] == "pause":
                parts.append(silence(seg.get("seconds", 1.0)))
                continue
            files = clip_paths.get(seg["id"]) or []
            if not files:
                continue
            gap = pause_between(prev, seg, s) if prev and prev["chapter"] == c else 0.0
            if gap:
                parts.append(silence(gap))
            for i, f in enumerate(files):
                audio, sr = sf.read(f, dtype="float32")
                if i:
                    parts.append(silence(s["pause_sentence"]))
                parts.append(fade(trim(audio)))
            prev = seg
        if not parts:
            continue
        parts.append(silence(s["pause_chapter"] / 2))
        audio = np.concatenate(parts)
        path = out_dir / f"chapter_{c:03d}.wav"
        sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
        chapters.append((chapter["title"], path, len(audio) / SAMPLE_RATE))
    return chapters


def _escape(text):
    for ch in ("\\", "=", ";", "#", "\n"):
        text = text.replace(ch, "\\" + ch)
    return text


def encode(chapters, out_path, title, s, author=""):
    """Join chapter WAVs into one file with chapter marks and even loudness."""
    out_path = Path(out_path)
    work = out_path.parent
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for _, p, _ in chapters))
    meta = [";FFMETADATA1", f"title={_escape(title)}", f"album={_escape(title)}"]
    if author:
        meta += [f"artist={_escape(author)}", f"album_artist={_escape(author)}"]
    meta.append("genre=Audiobook")
    t = 0.0
    for name, _, seconds in chapters:
        start, t = t, t + seconds
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(t * 1000)}",
                 f"title={_escape(name)}"]
    metafile = work / "chapters.txt"
    metafile.write_text("\n".join(meta) + "\n")

    fmt = s["output_format"]
    kbps = max(32, int(s["bitrate_kbps"]))
    codec = (["-c:a", "aac", "-b:a", f"{kbps}k", "-f", "mp4", "-movflags", "+faststart"] if fmt == "m4b"
             else ["-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-id3v2_version", "3", "-f", "mp3"])
    tmp = out_path.with_suffix(".part")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(metafile),
           "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
           "-af", f"loudnorm=I={s['loudness_lufs']}:TP=-2:LRA=11", "-ar", "44100", "-ac", "1",
           *codec, str(tmp)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg couldn't make the {fmt.upper()}: {result.stderr.strip()[-400:]}")
    tmp.replace(out_path)
    return out_path
