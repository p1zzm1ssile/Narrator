"""Turn a story's text into chapters, paragraphs and segments.

A segment is a run of narration or one piece of quoted dialogue. The LLM later
says who speaks each segment and how they feel, but it never rewrites the
text: what is read aloud is always exactly what was in the story.
"""

import re

HEADING = re.compile(
    r"^\s*(?:#{1,3}\s+\S.*"
    r"|(?:chapter|part|book)\s+[\w.\-]+(?:\s*[:.\-–—]\s*.*)?"
    r"|(?:prologue|epilogue|interlude|afterword|foreword)(?:\s*[:.\-–—]\s*.*)?)\s*$",
    re.IGNORECASE,
)
# “curly” quotes, "straight" quotes, and the dash dialogue some books use is left as narration
QUOTE = re.compile(r"“[^”]*”?|\"[^\"\n]*\"")
SENTENCE_END = re.compile(r"(?<=[.!?…])[\"”’)\]]*\s+")
WORDY = re.compile(r"[A-Za-z0-9]")


def _clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _paragraphs(block):
    lines = [l.strip() for l in block.split("\n")]
    paras, current = [], []
    for line in lines:
        if not line:
            if current:
                paras.append(" ".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paras.append(" ".join(current))
    # A file with no blank lines between paragraphs: treat each line as one
    if len(paras) == 1 and block.count("\n") > 3:
        paras = [l for l in lines if l]
    return [p for p in paras if WORDY.search(p)]


def split_quotes(paragraph):
    """Split a paragraph into (kind, text) pieces: narration and dialogue."""
    pieces, pos = [], 0
    for m in QUOTE.finditer(paragraph):
        before = paragraph[pos:m.start()]
        if before.strip():
            pieces.append(["narration", before.strip()])
        inner = m.group(0)
        if WORDY.search(inner):
            pieces.append(["dialogue", inner.strip()])
        pos = m.end()
    rest = paragraph[pos:]
    if rest.strip():
        pieces.append(["narration", rest.strip()])
    # Drop or merge bits with no words, like a lone comma between two quotes
    merged = []
    for kind, text in pieces:
        if not WORDY.search(text):
            if merged:
                merged[-1][1] = (merged[-1][1] + " " + text).strip()
            continue
        merged.append([kind, text])
    return [(k, t) for k, t in merged]


def parse(text, title="Story"):
    """Return {"chapters": [{"title"}], "segments": [...]}."""
    text = _clean(text)
    chapters, blocks = [], []
    current_title, current = None, []
    for line in text.split("\n"):
        if HEADING.match(line) and len(line) < 120:
            if current_title is not None or "".join(current).strip():
                chapters.append((current_title or title, current_title is not None))
                blocks.append("\n".join(current))
            current_title = line.strip().lstrip("#").strip()
            current = []
        else:
            current.append(line)
    chapters.append((current_title or title, current_title is not None))
    blocks.append("\n".join(current))

    segments, out_chapters = [], []
    for (title_text, is_heading), block in zip(chapters, blocks):
        paras = _paragraphs(block)
        if not paras and not is_heading:
            continue
        c = len(out_chapters)
        out_chapters.append({"title": title_text})
        if is_heading:
            segments.append(_segment(len(segments), c, -1, "heading", title_text))
        for p, para in enumerate(paras):
            for kind, piece in split_quotes(para):
                segments.append(_segment(len(segments), c, p, kind, piece))
    return {"chapters": out_chapters, "segments": segments}


def _segment(i, chapter, paragraph, kind, text):
    return {
        "id": i,
        "chapter": chapter,
        "paragraph": paragraph,
        "kind": kind,
        "text": text,
        "speaker": "Narrator" if kind != "dialogue" else "Unknown",
        "emotion": "neutral",
        "intensity": 0.5,
    }


def speakable(text):
    """The words to send to the voice: quote marks removed, spacing tidied."""
    text = text.strip()
    text = re.sub(r"^[“\"]|[”\"]$", "", text).strip()
    text = text.replace("—", ", ").replace("–", ", ")
    text = re.sub(r"\s*,\s*,", ",", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,")


def chunks(text, limit=280):
    """Split text into pieces of whole sentences no longer than limit.

    Chatterbox drifts on long inputs, so long sentences are split again at
    commas and semicolons, and as a last resort between words.
    """
    text = speakable(text)
    if not text:
        return []
    sentences = [s.strip() for s in SENTENCE_END.split(text) if s.strip()]
    parts = []
    for s in sentences:
        if len(s) <= limit:
            parts.append(s)
            continue
        clauses = re.split(r"(?<=[,;:])\s+", s)
        buf = ""
        for cl in clauses:
            while len(cl) > limit:
                cut = cl.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                if buf:
                    parts.append(buf)
                    buf = ""
                parts.append(cl[:cut].strip())
                cl = cl[cut:].strip()
            if buf and len(buf) + 1 + len(cl) > limit:
                parts.append(buf)
                buf = cl
            else:
                buf = (buf + " " + cl).strip()
        if buf:
            parts.append(buf)
    # Join very short sentences to their neighbour so the voice keeps its flow
    out = []
    for p in parts:
        if out and (len(out[-1]) < 40 or len(p) < 20) and len(out[-1]) + 1 + len(p) <= limit:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [p for p in out if WORDY.search(p)]


# ----- pre-tagged stories -----
#
#   CAST: Mira (female, young, British), Tom (male, old, Cornish)
#   ## Chapter 1: The Light Goes Out
#   [NARRATOR | calm, ominous] The storm had been building all afternoon.
#   [MIRA | scared, whispering] It's gone out.
#   [PAUSE 2s]
#
# Lines with no blank line between them are read as one passage, with short
# pauses; a blank line gives a paragraph pause.

TAGGED_LINE = re.compile(r"^\s*\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]*?)\s*)?\]\s*(.*)$")
PAUSE_LINE = re.compile(r"^\s*\[\s*PAUSE\s*(?:([\d.]+)\s*(?:s|sec|secs|seconds)?)?\s*\]\s*$", re.IGNORECASE)
CAST_LINE = re.compile(r"^\s*CAST\s*:\s*(.+)$", re.IGNORECASE)
CAST_ENTRY = re.compile(r"\s*([^,(]+?)\s*(?:\(([^)]*)\))?\s*(?:,|$)")
NARRATOR_NAMES = {"narrator", "narration", "narrate"}


def is_tagged(text):
    hits = 0
    for line in text.splitlines()[:200]:
        if PAUSE_LINE.match(line) or CAST_LINE.match(line):
            hits += 1
        else:
            m = TAGGED_LINE.match(line)
            if m and m.group(2) is not None and m.group(3).strip():
                hits += 1
        if hits >= 3:
            return True
    return False


def _cast(text):
    """CAST: Mira (female, young, British), Tom (male) -> {"Mira": {...}}"""
    cast = {}
    for m in CAST_ENTRY.finditer(text):
        name = m.group(1).strip().strip(".")
        if not name:
            continue
        hints = [h.strip() for h in (m.group(2) or "").split(",") if h.strip()]
        gender = next((h.lower() for h in hints if h.lower() in ("male", "female")), "unknown")
        if any(h.lower() in ("man", "boy", "he", "him") for h in hints):
            gender = "male"
        if any(h.lower() in ("woman", "girl", "she", "her") for h in hints):
            gender = "female"
        age = next((h.lower() for h in hints if h.lower() in ("child", "young", "adult", "middle-aged", "old", "elderly", "teen", "teenage")), "")
        rest = [h for h in hints if h.lower() not in (gender, age)]
        cast[name] = {"gender": gender, "age": age, "description": ", ".join(rest)}
    return cast


def parse_tagged(text, title="Story"):
    """Read a story written in the tagged format. Speakers and feelings come from the tags."""
    from core.tagger import feeling  # here to keep story.py free of tagger at import time

    text = _clean(text)
    chapters, segments, cast = [], [], {}
    paragraph, blank = 0, False

    def chapter_index():
        if not chapters:
            chapters.append({"title": title})
        return len(chapters) - 1

    def speaker_name(raw):
        if raw.lower() in NARRATOR_NAMES:
            return "Narrator"
        for known in cast:
            if known.lower() == raw.lower() or known.split()[0].lower() == raw.lower():
                return known
        return raw.title() if raw.isupper() else raw

    for line in text.split("\n"):
        if not line.strip():
            blank = True
            continue
        m = CAST_LINE.match(line)
        if m:
            cast.update(_cast(m.group(1)))
            continue
        if blank:
            paragraph += 1
            blank = False
        m = PAUSE_LINE.match(line)
        if m:
            seconds = float(m.group(1)) if m.group(1) else 1.0
            seg = _segment(len(segments), chapter_index(), paragraph, "pause", "")
            seg["seconds"] = max(0.0, min(seconds, 30.0))
            segments.append(seg)
            continue
        if HEADING.match(line) and len(line) < 120 and not TAGGED_LINE.match(line):
            heading = line.strip().lstrip("#").strip()
            if chapters and not any(s["chapter"] == len(chapters) - 1 for s in segments):
                chapters[-1] = {"title": heading}
            else:
                chapters.append({"title": heading})
            segments.append(_segment(len(segments), len(chapters) - 1, -1, "heading", heading))
            paragraph += 1
            continue
        m = TAGGED_LINE.match(line)
        if m and m.group(3).strip():
            who, words, spoken = speaker_name(m.group(1).strip()), m.group(2) or "", m.group(3).strip()
        else:
            who, words, spoken = "Narrator", "", line.strip()
        if not WORDY.search(spoken):
            continue
        kind = "narration" if who == "Narrator" else "dialogue"
        seg = _segment(len(segments), chapter_index(), paragraph, kind, spoken)
        seg["speaker"] = who
        seg["emotion"], seg["intensity"] = feeling(words)
        seg["direction"] = words
        segments.append(seg)
        if kind == "dialogue":
            cast.setdefault(who, {"gender": "unknown", "age": "", "description": ""})
    if not chapters:
        chapters.append({"title": title})
    return {"chapters": chapters, "segments": segments, "characters": cast, "pretagged": True}


def parse_any(text, title="Story"):
    return parse_tagged(text, title) if is_tagged(text) else parse(text, title)
