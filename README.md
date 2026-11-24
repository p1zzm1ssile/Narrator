<p align="center"><img src="src/web/icon.png" alt="" width="96"></p>

# Narrator

Narrator turns a story into an audiobook read by a cast of expressive voices. Each character gets their own voice, and every line is acted out with the feeling it was written with: whispered, shouted, scared, amused.

It runs as one Docker container on a machine with an NVIDIA GPU, such as an Unraid server.

1. **Who says what, and how.** Your own [Ollama](https://ollama.com) reads the story and marks who speaks each line and how they feel. It only labels the lines, it never rewrites them, so what you hear is exactly what was written. Without Ollama, speakers are guessed from tags like *"said Mira"*. Or write the story with the tags already in (see [Writing stories for Narrator](#writing-stories-for-narrator)) and this step is skipped.
2. **The voices.** [Chatterbox](https://github.com/resemble-ai/chatterbox), running on your GPU inside this container, acts out each line. It can copy any voice from a 10 to 20 second clip. Your [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) adds its voices too, which are flatter but very fast, and stands in for any line Chatterbox keeps getting wrong.
3. **The check.** Whisper listens back to every line and renders it again if the words don't match. It runs on the CPU, so the GPU stays free for the voices.
4. **The audiobook.** ffmpeg joins the lines with natural pauses and makes an M4B audiobook with chapters, or an MP3.

## Picking voices

Characters are always cast with expressive Chatterbox voices: your own, then a Chatterbox copy of each of your English Kokoro voices. For a copy, Kokoro reads a short passage once, and Chatterbox copies that voice and acts with it. So every character can sound different and still show feeling. Plain Kokoro voices are flat, so they're only used if you pick one by hand.

The **Voices** page lists every Chatterbox voice and every voice your Kokoro has, with its accent and gender. Press **Play** to hear each one read the same few lines, and **Use as narrator** on the one you like. Add your own voices there by uploading a short clip.

When a story has been cast, its page lists the characters with a voice dropdown for each, and a play button to hear their first line in that voice. Every line is listed below with who says it and how they feel, so you can fix anything before pressing **Render**. Nothing needs setting up in Kokoro: give Narrator its address in Settings and it reads the voice list itself.

## Unraid

1. Install the **Nvidia Driver** plugin from Community Applications, if you haven't already.
2. Add the container from the template ([unraid/narrator.xml](unraid/narrator.xml), also in [p1zzm1ssile/unraid-templates](https://github.com/p1zzm1ssile/unraid-templates)). It sets `--runtime=nvidia` and `NVIDIA_VISIBLE_DEVICES=all`.
3. Open the web UI on port **9895**, and in **Settings** fill in your Ollama and Kokoro addresses and press **Test** on each.

The first time a voice is used, Narrator downloads the Chatterbox model (about 3 GB) and the Whisper model into `/config/models`. That takes a few minutes, once.

**GPU memory.** Chatterbox uses about 4 to 5 GB of an 8 GB card. If Ollama runs on the same GPU, the LLM step runs before rendering starts and Chatterbox is freed after a few idle minutes (*Settings > Free the GPU after*), so they take turns. A 7B or 8B model such as `qwen2.5:7b` or `llama3.1:8b` fits beside it. Whisper runs on the CPU by default.

**Supported cards.** GTX 10-series to RTX 40-series (CUDA 12.4). RTX 50-series cards need a newer PyTorch than Chatterbox currently allows. Without a GPU it still works, but many times slower.

## Docker Compose

See [docker-compose.yml](docker-compose.yml). It needs the NVIDIA Container Toolkit on the host.

## Settings

All settings are in the web UI and saved in `/config/settings.json`. These environment variables fill them in on first start:

| Variable | What it's for |
|---|---|
| `OLLAMA_URL`, `OLLAMA_MODEL` | Your Ollama, e.g. `http://192.168.1.10:11434`, and the model (`qwen2.5:7b` by default) |
| `KOKORO_URL` | Your Kokoro-FastAPI, e.g. `http://192.168.1.10:8880` |
| `WHISPER_MODE` | `builtin` (faster-whisper on the CPU, the default), `external` (your [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice), set its address in the web UI) or `off` |
| `NARRATOR_VOICE` | e.g. `chatterbox:default` or `kokoro:bm_george` |
| `OUTPUT_FORMAT` | `m4b` (default) or `mp3` |
| `WEB_PASSWORD` | Ask for this password in the browser and the API |

## How feelings become a performance

Each line gets one of 13 feelings (neutral, happy, excited, amused, tender, sad, angry, fearful, surprised, tense, serious, sarcastic, whisper) and a strength from 0 to 1. For Chatterbox voices, the feeling sets how much it acts (*exaggeration*) and how freely the pace follows the feeling (*cfg weight*). Kokoro voices only speed up or slow down. *Narrator expressiveness* and *Character expressiveness* in Settings turn the acting up or down overall: by default the narrator reads with half the feeling the characters get.

## Writing stories for Narrator

Plain stories work: dialogue in quotes goes to the characters, everything else to the narrator, and lines like `Chapter 1`, `Prologue` or `# Heading` start chapters.

For full control, write the story in the tagged format. Narrator spots it by itself and skips the LLM step:

```
CAST: Mira (female, child, British), Tom (male, old, Cornish, gruff but kind)

## Chapter 1: The Light Goes Out

[NARRATOR | calm, ominous] The storm had been building all afternoon.
[MIRA | panicked, very scared] It's gone out! Grandpa, the light's gone out!
[TOM | gentle, steady] Then we'd best go and light it again.

[MIRA | whispering, afraid] In this?
[PAUSE 1.5s]
[TOM | warm, slightly amused] Ships don't wait for good weather, love.
```

- `CAST:` is optional. Gender, age and accent words help pick a voice: *British*, *Scottish* or *Cornish* prefer Kokoro's British voices, for example.
- Each line is `[SPEAKER | how they say it] the words`. Dialogue lines hold only the spoken words, with no quote marks or *"she said"*.
- Everything after a tag, up to the next tag, is read by that speaker, so a tag can also sit on its own line, or at the end of the line before (Grok often writes it that way).
- The feeling words are free-form. *whispering* or *hushed* always whisper; otherwise the first feeling named wins, and *very*, *shouting* or *slightly* make it stronger or softer.
- A change of speaker gets a short pause, and the same speaker carrying on gets a paragraph pause. A blank line inside one speaker's text starts a new paragraph. `[PAUSE 2s]` adds a pause of your own.

A prompt you can give Grok, ChatGPT or Claude to write stories this way is in [docs/story-prompt.md](docs/story-prompt.md). There's a short example in [examples/the-lighthouse-tagged.txt](examples/the-lighthouse-tagged.txt).

## API

Everything the web UI does is in the API. With `WEB_PASSWORD` set, send it as HTTP Basic auth (any user name) or `Authorization: Bearer <password>`.

| | |
|---|---|
| `POST /api/jobs` | Start a story. Form fields: `file` (.txt or .md) or `text`, and optional `title`, `author`, `format` (`m4b`/`mp3`), `review` (`0` renders straight away, `1` stops for you to check the cast) |
| `GET /api/jobs`, `GET /api/jobs/{id}` | Progress: `status` is queued, tagging, review, rendering, mixing, done, failed or stopped |
| `GET /api/jobs/{id}/script`, `PUT /api/jobs/{id}/script` | The cast and lines. PUT `{"characters": {"Tom": {"voice": "kokoro:bm_george"}}, "segments": [{"id": 4, "emotion": "angry", "intensity": 0.8}], "narrator_voice": "..."}` |
| `POST /api/jobs/{id}/render`, `/retag`, `/stop`, `DELETE /api/jobs/{id}` | Render, read and cast again, stop, delete |
| `GET /api/jobs/{id}/audio` | Download the audiobook |
| `GET /api/voices` | Chatterbox and Kokoro voices, and the feelings |
| `GET /api/voices/sample?voice=kokoro:af_bella` | A sample of a voice (WAV) |
| `POST /api/voices` | Add a Chatterbox voice: form fields `name`, `gender`, `description`, `file` |
| `POST /api/preview` | Speak a line: `{"text": "...", "voice": "chatterbox:default", "emotion": "angry", "intensity": 0.8}` returns a WAV |
| `GET/PUT /api/settings` | Settings |

```sh
curl -F file=@story.txt -F review=0 http://tower:9895/api/jobs
```

## Notes

- Chatterbox adds Resemble AI's inaudible Perth watermark to everything it speaks.
- Only copy voices you have the right to use.
- Narrator reads English. Chatterbox is English-only in this version.

## Licence

MIT. Chatterbox is MIT, Kokoro is Apache 2.0, faster-whisper is MIT.
