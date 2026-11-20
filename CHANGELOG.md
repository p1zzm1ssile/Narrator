# Changelog

## 0.1.0

First version.

- **Stories to audiobooks.** Upload a .txt or .md story, or paste it, and get an M4B audiobook with chapters or an MP3.
- **Who says what, and how.** Your Ollama marks who speaks each line and how they feel, without changing a word of the story. Without Ollama, speakers are guessed from tags like "said Mira".
- **Pre-tagged stories.** Stories written as `[SPEAKER | how it is said] the words`, with an optional `CAST:` line and `[PAUSE 2s]`, skip the LLM step. A prompt for Grok or ChatGPT is in docs/story-prompt.md.
- **Expressive voices.** Chatterbox on the GPU acts each line with its feeling, and copies a voice from a 10 to 20 second clip. Your Kokoro-FastAPI voices are offered too, and stand in for lines Chatterbox keeps getting wrong.
- **Voices page.** Hear a sample of every Chatterbox and Kokoro voice, pick the narrator, add your own voices, and try any line with any feeling.
- **Check before rendering.** Each story stops after casting so you can change voices, speakers and feelings, and hear any line, before pressing Render.
- **Whisper check.** Every line is listened back to and rendered again if the words don't match.
- **Picks up where it left off.** Rendered lines are kept, so a restart or a change only renders what's new.
- **Frees the GPU** after a few idle minutes so Ollama and Kokoro can use it.
- Unraid template with GPU passthrough, Docker Compose example, and an API for everything.
