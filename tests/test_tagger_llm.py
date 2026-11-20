"""The LLM step, with a stand-in Ollama."""

import json

from core import story, tagger


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": json.dumps(self.payload)}}


def test_llm_labels_but_never_rewrites(monkeypatch):
    d = story.parse('"Hello," said the old man.\n\n"Go away!" she shouted.', "T")
    d["characters"] = {}
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["body"] = json
        return FakeResponse({
            "characters": [{"name": "Old Man", "gender": "male", "age": "old", "description": "gravelly"},
                           {"name": "Ella", "gender": "female"}],
            "segments": [
                {"id": 0, "speaker": "Old Man", "emotion": "tender", "intensity": 0.4, "text": "IGNORED"},
                {"id": 1, "speaker": "Ella", "emotion": "neutral", "intensity": 0.2},
                {"id": 2, "speaker": "ella", "emotion": "angry", "intensity": 1.4},
                {"id": 3, "speaker": "Narrator", "emotion": "made-up", "intensity": 0.5},
            ],
        })

    monkeypatch.setattr(tagger.requests, "post", fake_post)
    texts = [s["text"] for s in d["segments"]]
    tagger.tag(d, {"ollama_url": "http://ollama:11434", "ollama_model": "qwen2.5:7b"})
    assert sent["body"]["format"]["required"] == ["characters", "segments"]
    segs = d["segments"]
    assert [s["text"] for s in segs] == texts
    assert segs[0]["speaker"] == "Old Man" and segs[0]["emotion"] == "tender"
    assert segs[1]["speaker"] == "Narrator"          # narration stays with the narrator
    assert segs[2]["speaker"] == "Ella"              # spelling matched to the known name
    assert segs[2]["intensity"] == 1.0               # clamped
    assert segs[3]["emotion"] == "neutral"           # unknown emotions are dropped
    assert d["characters"]["Old Man"]["gender"] == "male"


def test_llm_failure_falls_back_to_rules(monkeypatch):
    d = story.parse('"Hi," said Mira.', "T")
    d["characters"] = {}

    def broken(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(tagger.requests, "post", broken)
    tagger.tag(d, {"ollama_url": "http://ollama:11434", "ollama_model": "x"})
    assert d["segments"][0]["speaker"] == "Mira"
