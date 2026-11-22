# Changelog

## 0.1.1

- **Grok's tag layout is read properly.** A `[SPEAKER | feeling]` tag can now sit anywhere: at the start of its line, on a line of its own, or at the end of the line before, as Grok writes it. Everything after a tag, up to the next one, belongs to that speaker. Before, stories written that way were read as plain text and every line went to the narrator. Press **Read and cast again** on a story you've already added to read it with the fix.
- **A new story is cast straight away**, even while another is rendering. Before, it waited silently behind the render, so nothing seemed to happen after uploading. A story waiting to render now says which one it's waiting for.
- **Render progress in the log**: what's being rendered and with which voices, then progress with a percentage and time left every 30 seconds, the cast, when it's joining the audio, and when it's done. A line that takes over 90 seconds is logged, and a failed line says which line and voice. Stories picked up after a restart are logged too.
- **No more "Sampling: 19%" in the log.** That was Chatterbox's own bar for each line, which stops early when the line ends, so it always looked stuck. It's hidden now.
- **Freeing the GPU never waits on a line being rendered**, and can't unload Chatterbox straight after it loads.
- Log lines are no longer printed twice.
- The story prompt in docs/story-prompt.md is stricter about where tags go.

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
