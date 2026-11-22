"""Stories written in the tagged format (for example by Grok or ChatGPT) skip the LLM step."""

from pathlib import Path

from core import story, tagger, voices

TAGGED = (Path(__file__).parent.parent / "examples" / "the-lighthouse-tagged.txt").read_text()
PLAIN = (Path(__file__).parent.parent / "examples" / "the-lighthouse.txt").read_text()


def test_detects_tagged_and_plain():
    assert story.is_tagged(TAGGED)
    assert not story.is_tagged(PLAIN)


def test_parse_tagged():
    d = story.parse_any(TAGGED, "The Lighthouse")
    assert d["pretagged"]
    assert [c["title"] for c in d["chapters"]] == ["Chapter 1: The Light Goes Out", "Chapter 2: The Climb"]
    assert d["characters"]["Mira"] == {"gender": "female", "age": "child", "description": "British"}
    assert d["characters"]["Tom"]["gender"] == "male"
    assert "Cornish" in d["characters"]["Tom"]["description"]
    by_text = {s["text"]: s for s in d["segments"] if s["text"]}
    whisper = by_text["In this?"]
    assert (whisper["speaker"], whisper["kind"], whisper["emotion"]) == ("Mira", "dialogue", "whisper")
    panicked = by_text["It's gone out! Grandpa, the light's gone out!"]
    assert panicked["emotion"] == "fearful" and panicked["intensity"] >= 0.8
    assert by_text["Then we'd best go and light it again."]["speaker"] == "Tom"
    narration = by_text[[t for t in by_text if t.startswith("The storm")][0]]
    assert (narration["speaker"], narration["emotion"]) == ("Narrator", "tense")
    pauses = [s for s in d["segments"] if s["kind"] == "pause"]
    assert [p["seconds"] for p in pauses] == [1.5]
    # No tag text is read aloud
    assert not any("[" in s["text"] or "|" in s["text"] for s in d["segments"])


def test_text_after_a_tag_belongs_to_it_until_the_next_tag():
    text = "[NARRATOR | calm] One.\n[BOB | angry] Two!\nStill Bob.\n\nBob again, new paragraph.\n[PAUSE]\nAfter the pause."
    d = story.parse_tagged(text, "T")
    assert [(s["kind"], s["speaker"], s["text"]) for s in d["segments"]] == [
        ("narration", "Narrator", "One."), ("dialogue", "Bob", "Two! Still Bob."),
        ("dialogue", "Bob", "Bob again, new paragraph."), ("pause", "Narrator", ""),
        ("narration", "Narrator", "After the pause.")]
    assert d["segments"][3]["seconds"] == 1.0


def test_grok_layout_tag_at_end_of_previous_line():
    """Grok puts each tag at the end of the line before, or on its own line, then a blank line."""
    text = (
        "[LAUREN | nervous, quiet]\n\n"
        "I still feel strange doing this. With you watching.[NARRATOR | gentle]\n\n"
        "Marcus stepped closer. He did not rush her.[MARCUS | calm]\n\n"
        "You can still leave. Door is right there.\n"
    )
    assert story.is_tagged(text)
    d = story.parse_any(text, "T")
    assert [(s["speaker"], s["text"]) for s in d["segments"]] == [
        ("Lauren", "I still feel strange doing this. With you watching."),
        ("Narrator", "Marcus stepped closer. He did not rush her."),
        ("Marcus", "You can still leave. Door is right there."),
    ]
    assert d["segments"][0]["emotion"] == "fearful"
    assert set(d["characters"]) == {"Lauren", "Marcus"}


def test_feeling_words():
    assert tagger.feeling("scared, whispering") == ("whisper", 0.6)
    assert tagger.feeling("very angry, shouting")[0] == "angry"
    assert tagger.feeling("calm, ominous")[0] == "tense"
    assert tagger.feeling("calm") == ("neutral", 0.5)
    assert tagger.feeling("slightly amused") == ("amused", 0.35)


def test_cast_hints_pick_accent():
    chars = {"Tom": {"gender": "male", "description": "Cornish", "lines": 3},
             "Mira": {"gender": "female", "description": "American", "lines": 4}}
    voices.auto_cast(chars, "chatterbox:default", ["af_bella", "am_adam", "bf_emma", "bm_george"])
    # Characters get expressive Chatterbox copies of Kokoro voices, never flat Kokoro
    assert chars["Tom"]["voice"] == "chatterbox:k-bm_george"
    assert chars["Mira"]["voice"] == "chatterbox:k-af_bella"


def test_kokoro_copy_is_made_once_from_kokoro(tmp_path, monkeypatch):
    import numpy as np
    from core import config, engines
    monkeypatch.setattr(config, "VOICES_DIR", tmp_path)
    calls = []

    def fake_kokoro(text, voice, p):
        calls.append(voice)
        return np.zeros(24000, dtype=np.float32)

    monkeypatch.setattr(engines.kokoro, "speak", fake_kokoro)
    assert voices.path("k-bm_george") == tmp_path / "k-bm_george.wav"
    assert voices.path("k-bm_george") == tmp_path / "k-bm_george.wav"
    assert calls == ["bm_george"]
    assert voices.path("k-../etc") is None
    lib = {v["id"]: v for v in voices.library([])}
    assert lib["chatterbox:k-bm_george"]["gender"] == "male"
    assert lib["chatterbox:k-bm_george"]["accent"] == "British"
