from pathlib import Path

from core import engines, story, tagger

EXAMPLE = (Path(__file__).parent.parent / "examples" / "the-lighthouse.txt").read_text()


def test_chapters_and_dialogue_split():
    d = story.parse(EXAMPLE, "The Lighthouse")
    assert [c["title"] for c in d["chapters"]] == ["Chapter 1: The Light Goes Out", "Chapter 2: The Climb"]
    kinds = [s["kind"] for s in d["segments"]]
    assert kinds[0] == "heading"
    assert kinds.count("dialogue") == 7
    # The text read aloud is exactly the story's text
    joined = " ".join(s["text"] for s in d["segments"])
    for word in ("storm", "Grandpa", "ghosts", "friendly"):
        assert word in joined


def test_story_without_headings_is_one_chapter():
    d = story.parse("Once upon a time.\n\n\"Hello,\" she said.", "Tiny")
    assert d["chapters"] == [{"title": "Tiny"}]
    assert [s["kind"] for s in d["segments"]] == ["narration", "dialogue", "narration"]


def test_guess_speakers_without_llm():
    d = story.parse(EXAMPLE, "The Lighthouse")
    chars = {}
    tagger.guess(d["segments"], chars)
    speakers = [s["speaker"] for s in d["segments"] if s["kind"] == "dialogue"]
    assert speakers == ["Mira", "Mira", "Tom", "Mira", "Tom", "Mira", "Tom"]
    assert chars["Mira"]["gender"] == "female"
    assert chars["Tom"]["gender"] == "male"
    whisper = [s for s in d["segments"] if s["text"] == '"In this?"'][0]
    assert whisper["emotion"] == "whisper"


def test_chunks_respect_limit():
    long = "This is a fairly long sentence that keeps on going, and going, and going. " * 20
    parts = story.chunks(long, limit=200)
    assert all(len(p) <= 200 for p in parts)
    assert " ".join(parts).split() == story.speakable(long).split()


def test_speakable_strips_quotes():
    assert story.speakable("“Run—now!”") == "Run, now!"


def test_emotion_params():
    calm = engines.params("neutral", 0.5, 1.0)
    angry = engines.params("angry", 1.0, 1.0)
    flat = engines.params("angry", 1.0, 0.0)
    assert angry["exaggeration"] > calm["exaggeration"] == flat["exaggeration"]
    assert angry["cfg_weight"] < calm["cfg_weight"]
    assert 0.25 <= angry["exaggeration"] <= 1.3
