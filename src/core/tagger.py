"""Mark who speaks each segment and how they feel.

The LLM (any Ollama-compatible server) only labels numbered segments; it never
returns text to be read, so it can't change the story. When no LLM is set up,
or it gives an answer that can't be used, simple rules fill in instead.
"""

import json
import re

import requests

from utils.logger import log

EMOTIONS = [
    "neutral", "happy", "excited", "amused", "tender", "sad", "angry",
    "fearful", "surprised", "tense", "serious", "sarcastic", "whisper",
]

BATCH = 40

SYSTEM = """You are a casting director preparing a story for an audiobook read by several voice actors.
You get numbered segments of the story. Segments marked DIALOGUE are spoken by a character; NARRATION and HEADING are read by the narrator.
For every segment, give:
- speaker: for DIALOGUE, the character's name exactly as the story calls them (use the same spelling every time; use "Unknown" only if it truly can't be worked out from the context). For NARRATION and HEADING always "Narrator".
- emotion: one of %s. Use "whisper" for whispered or muttered lines. For narration, pick the mood of the passage.
- intensity: 0.0 to 1.0, how strongly the emotion should be acted.
Also list every character who speaks, with gender (male, female or unknown), rough age (child, young, adult, old) and a few words describing their voice or manner.
Answer with JSON only.""" % ", ".join(EMOTIONS)

SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "gender": {"type": "string"},
                    "age": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "gender"],
            },
        },
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "emotion": {"type": "string", "enum": EMOTIONS},
                    "intensity": {"type": "number"},
                },
                "required": ["id", "speaker", "emotion", "intensity"],
            },
        },
    },
    "required": ["characters", "segments"],
}


def tag(script, settings, progress=None, cancelled=lambda: False):
    """Fill in speaker, emotion and intensity on script["segments"] and script["characters"]."""
    segments = [s for s in script["segments"] if s["kind"] != "pause"]
    characters = script.setdefault("characters", {})
    url, model = settings["ollama_url"], settings["ollama_model"]
    if not url:
        log.info("No Ollama address set, guessing speakers and feelings from the text")
        guess(segments, characters)
        return script

    total = len(segments)
    for start in range(0, total, BATCH):
        if cancelled():
            return script
        batch = segments[start:start + BATCH]
        before = segments[max(0, start - 6):start]
        try:
            answer = _ask(url, model, batch, before, characters)
            _apply(answer, batch, characters)
        except Exception as e:  # noqa: BLE001 - any LLM failure falls back to rules
            log.warning(f"The LLM couldn't tag segments {start}-{start + len(batch) - 1} ({e}), guessing them instead")
            guess(batch, characters)
        if progress:
            progress(min(start + BATCH, total), total)
    _fill_unknown_characters(segments, characters)
    return script


def _describe(seg):
    return f'[{seg["id"]}] {seg["kind"].upper()}: {seg["text"]}'


def _ask(url, model, batch, before, characters):
    known = ""
    if characters:
        known = "Characters so far (keep these names):\n" + "\n".join(
            f'- {n} ({c.get("gender", "unknown")}, {c.get("age", "")}): {c.get("description", "")}'
            for n, c in characters.items() if n not in ("Narrator", "Unknown")
        ) + "\n\n"
    context = ""
    if before:
        context = "Earlier context (already done, don't tag):\n" + "\n".join(
            f'{_describe(s)} -> {s["speaker"]}' for s in before) + "\n\n"
    prompt = known + context + "Tag these segments:\n" + "\n".join(_describe(s) for s in batch)
    body = {
        "model": model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    }
    last = None
    for _ in range(2):
        r = requests.post(f"{url}/api/chat", json=body, timeout=600)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except ValueError as e:
            last = e
    raise ValueError(f"answer wasn't JSON ({last})")


def _apply(answer, batch, characters):
    for c in answer.get("characters") or []:
        name = (c.get("name") or "").strip()
        if not name or name.lower() in ("narrator", "unknown"):
            continue
        entry = characters.setdefault(name, {})
        for key in ("gender", "age", "description"):
            if c.get(key) and not entry.get(key):
                entry[key] = str(c[key]).strip().lower() if key == "gender" else str(c[key]).strip()
    by_id = {s["id"]: s for s in batch}
    seen = set()
    for item in answer.get("segments") or []:
        seg = by_id.get(item.get("id"))
        if seg is None:
            continue
        seen.add(seg["id"])
        emotion = str(item.get("emotion", "neutral")).lower()
        seg["emotion"] = emotion if emotion in EMOTIONS else "neutral"
        try:
            seg["intensity"] = max(0.0, min(1.0, float(item.get("intensity", 0.5))))
        except (TypeError, ValueError):
            seg["intensity"] = 0.5
        if seg["kind"] == "dialogue":
            speaker = (item.get("speaker") or "Unknown").strip() or "Unknown"
            if speaker.lower() == "narrator":
                speaker = "Unknown"
            seg["speaker"] = _match_name(speaker, characters)
        else:
            seg["speaker"] = "Narrator"
    missing = [s for s in batch if s["id"] not in seen]
    if missing:
        guess(missing, characters)


def _match_name(name, characters):
    """Use the known spelling when the LLM writes 'Mr Hale' for 'Mr. Hale' or 'hale'."""
    if name in characters:
        return name
    key = re.sub(r"[^a-z]", "", name.lower())
    for known in characters:
        if re.sub(r"[^a-z]", "", known.lower()) == key:
            return known
    return name


def _fill_unknown_characters(segments, characters):
    for s in segments:
        if s["kind"] == "dialogue" and s["speaker"] not in characters:
            characters.setdefault(s["speaker"], {"gender": "unknown"})


# ----- rules used without an LLM -----

SAID = (r"(?:said|says|asked|asks|replied|replies|shouted|yelled|cried|whispered|muttered|"
        r"murmured|called|answered|added|snapped|laughed|sighed|growled|hissed|screamed|exclaimed|"
        r"continued|insisted|told|began|demanded|pleaded|breathed)")
AFTER = re.compile(r"^\s*,?\s*(?:" + SAID + r"\s+((?:[A-Z][\w'\-]+)(?:\s+[A-Z][\w'\-]+)?)"
                   r"|((?:[A-Z][\w'\-]+)(?:\s+[A-Z][\w'\-]+)?)\s+" + SAID + r")\b")
BEFORE = re.compile(r"((?:[A-Z][\w'\-]+)(?:\s+[A-Z][\w'\-]+)?)\s+" + SAID + r"\s*[,:]?\s*$")
PRONOUN = {"he": "male", "him": "male", "his": "male", "she": "female", "her": "female"}
NAME = re.compile(r"\b[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)?\b")
TITLES = {"Old", "Young", "Little", "Big", "Mr", "Mrs", "Ms", "Miss", "Dr", "Sir", "Lady", "Lord", "Uncle", "Aunt"}
NOT_NAMES = {"He", "She", "They", "I", "We", "You", "It", "The", "A", "An", "And", "But", "Then", "His", "Her",
             "Their", "There", "This", "That", "When", "What", "Where", "Somewhere", "Just", "Nothing", "Grandpa",
             "Grandma", "Mum", "Mom", "Dad", "Yes", "No", "Oh", "Well", "So", "If", "In", "On", "At", "As"}
CUES = [
    (re.compile(r"\b(whisper|murmur|mutter|breath)", re.I), "whisper", 0.6),
    (re.compile(r"\b(shout|yell|scream|roar|snap|snarl|growl|bellow)", re.I), "angry", 0.8),
    (re.compile(r"\b(laugh|grin|giggle|chuckle)", re.I), "amused", 0.6),
    (re.compile(r"\b(sob|cried|tears|weep|sigh)", re.I), "sad", 0.6),
    (re.compile(r"\b(gasp|stammer|trembl|shak)", re.I), "fearful", 0.6),
]


def _name_in(text, characters):
    """The first character name in a piece of narration, preferring names already known."""
    for known in characters:
        if known not in ("Unknown", "Narrator") and re.search(r"\b" + re.escape(known.split()[-1]) + r"\b", text):
            return known
    for m in NAME.finditer(text):
        words = [w for w in m.group(0).split() if w not in NOT_NAMES and w not in TITLES]
        if words:
            return " ".join(words)
    return None


def guess(segments, characters):
    last_speakers = []
    para_speaker = {}
    for i, seg in enumerate(segments):
        if seg["kind"] != "dialogue":
            seg["speaker"] = "Narrator"
            continue
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        prev = segments[i - 1] if i > 0 else None
        name, around = None, ""
        if nxt and nxt["kind"] == "narration" and nxt["paragraph"] == seg["paragraph"]:
            around = nxt["text"]
            m = AFTER.match(nxt["text"])
            if m:
                name = m.group(1) or m.group(2)
        if not name and prev and prev["kind"] == "narration" and prev["paragraph"] == seg["paragraph"]:
            around = prev["text"] + " " + around
            m = BEFORE.search(prev["text"])
            if m:
                name = m.group(1)
        if name and name.split()[0] in NOT_NAMES:
            name = None
        key = (seg["chapter"], seg["paragraph"])
        if not name:
            # Another quote in the same paragraph already has a speaker
            name = para_speaker.get(key)
        if not name:
            # An action beat in the same paragraph: "Tom laughed. "Just the wind.""
            beat = " ".join(s["text"] for s in segments
                            if s["kind"] == "narration" and (s["chapter"], s["paragraph"]) == key)
            name = _name_in(beat, characters)
        if not name and len(last_speakers) >= 2 and prev and prev["paragraph"] != seg["paragraph"]:
            # Back-and-forth conversation: alternate between the last two speakers
            name = last_speakers[-2]
        name = _match_name(name or "Unknown", characters)
        seg["speaker"] = name
        if name != "Unknown":
            para_speaker[key] = name
        if name != "Unknown":
            entry = characters.setdefault(name, {"gender": "unknown"})
            if entry.get("gender", "unknown") == "unknown":
                for word in re.findall(r"\b(he|him|his|she|her)\b", around.lower()):
                    entry["gender"] = PRONOUN[word]
                    break
            if not last_speakers or last_speakers[-1] != name:
                last_speakers.append(name)
        seg["emotion"], seg["intensity"] = "neutral", 0.5
        for pattern, emotion, intensity in CUES:
            if pattern.search(around):
                seg["emotion"], seg["intensity"] = emotion, intensity
                break
        else:
            if seg["text"].rstrip("”\"").endswith("!"):
                seg["emotion"], seg["intensity"] = "excited", 0.6
    _fill_unknown_characters(segments, characters)


# ----- stage directions from a pre-tagged story, like "scared, whispering" -----

FEELING_WORDS = [
    ("whisper", ("whisper", "hushed", "murmur", "mutter", "under her breath", "under his breath", "breathy", "barely audible")),
    ("angry", ("shout", "yell", "scream", "furious", "angry", "anger", "rage", "snarl", "roar", "bellow", "livid", "irritated", "annoyed", "snapping")),
    ("fearful", ("scared", "afraid", "fear", "terrified", "nervous", "anxious", "panic", "trembling", "frightened", "shaky", "uneasy")),
    ("excited", ("excited", "thrilled", "eager", "breathless", "elated", "triumphant", "enthusiastic")),
    ("surprised", ("surprise", "shock", "astonish", "startled", "stunned", "disbelief", "amazed")),
    ("sad", ("sad", "grief", "grieving", "mournful", "melancholy", "crying", "tearful", "sorrow", "weary", "tired", "heartbroken", "defeated", "resigned", "wistful", "lonely")),
    ("tender", ("tender", "gentle", "soft", "loving", "kind", "soothing", "reassuring", "affectionate", "comforting", "warmly")),
    ("amused", ("amused", "laugh", "playful", "teasing", "wry", "chuckl", "giggl", "mischievous", "smirk")),
    ("happy", ("happy", "cheerful", "joy", "delighted", "bright", "warm", "relieved", "proud", "content")),
    ("sarcastic", ("sarcastic", "mocking", "sneer", "dry", "scornful", "contempt", "condescending")),
    ("tense", ("tense", "ominous", "suspense", "dread", "urgent", "menacing", "foreboding", "uneasy", "sinister", "hurried")),
    ("serious", ("serious", "grave", "solemn", "stern", "firm", "cold", "flat", "measured", "authoritative", "formal")),
    ("neutral", ("calm", "neutral", "matter-of-fact", "steady", "even", "plain", "conversational")),
]
STRONGER = ("very", "extremely", "intense", "barely contained", "furious", "terrified", "shouting", "screaming",
            "sobbing", "hysterical", "desperate", "overwhelmed", "deeply")
SOFTER = ("slightly", "a little", "a bit", "mildly", "quietly", "faintly", "subtle", "somewhat")


def feeling(words):
    """Turn free-form stage directions into one of EMOTIONS and a strength from 0 to 1."""
    text = (words or "").lower()
    if not text.strip():
        return "neutral", 0.5
    # Whispering is how a line is delivered, so it wins over the feeling.
    # Otherwise the first feeling named wins, and "calm" only counts on its own.
    emotion, first = "neutral", len(text) + 1
    for name, keys in FEELING_WORDS:
        if name == "neutral":
            continue
        found = [i for i in (text.find(k) for k in keys) if i >= 0]
        if not found:
            continue
        if name == "whisper":
            emotion = "whisper"
            break
        if min(found) < first:
            emotion, first = name, min(found)
    intensity = 0.6 if emotion != "neutral" else 0.5
    if any(k in text for k in STRONGER):
        intensity += 0.3
    if any(k in text for k in SOFTER):
        intensity -= 0.25
    if emotion == "angry" and any(k in text for k in ("shout", "yell", "scream", "roar", "bellow")):
        intensity = max(intensity, 0.85)
    return emotion, round(max(0.1, min(1.0, intensity)), 2)
