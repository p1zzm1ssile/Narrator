# A prompt for writing stories Narrator can read

Paste this into Grok, ChatGPT or Claude, then add what the story is about at the end. Save the answer as a `.txt` file and upload it to Narrator. Narrator sees the tags, skips its own LLM step, and casts the characters from the `CAST:` line.

```
Write a story for an audiobook that will be read by several voice actors. Write it in exactly this format and nothing else, with no notes before or after it.

1. Start with one line listing every character who speaks:
   CAST: Name (gender, age, accent, a few words about their voice), Name (...), ...
   Use male or female for gender, and child, young, adult or old for age. Example:
   CAST: Mira (female, child, British, bright and quick), Tom (male, old, Cornish, gruff but kind)

2. Start each chapter with a line like:
   ## Chapter 1: The Light Goes Out

3. Write every line of the story as:
   [SPEAKER | how it is said] the words
   - SPEAKER is NARRATOR for narration, or the character's name in capitals, spelled the same every time, for dialogue.
   - "How it is said" is one to three words of direction, like: calm, ominous / scared, whispering / very angry, shouting / warm, slightly amused / tense / sad, tired / sarcastic. Use "whispering" for anything whispered or muttered.
   - A dialogue line holds only the words the character says aloud: no quote marks, no "she said", no actions. Put actions and "she said" in a NARRATOR line instead, or leave them out.
   - Keep each line to one to three sentences. Split long passages into several lines.

4. Lines that follow straight on from each other go on consecutive lines. Leave a blank line where a new paragraph or beat starts.

5. For a dramatic silence, put [PAUSE 2s] on its own line (any number of seconds).

6. Give the narrator directions too, so the narration follows the mood: calm, tense, ominous, gentle, urgent.

7. Don't use stage directions in brackets inside the words, sound effects, or emojis. Write numbers as words.

The story:
```

## Example of the result

```
CAST: Mira (female, child, British), Tom (male, old, Cornish, gruff but kind)

## Chapter 1: The Light Goes Out

[NARRATOR | calm, ominous] The storm had been building all afternoon, and by the time the sun went down the sea was throwing itself at the rocks.
[MIRA | panicked, very scared] It's gone out! Grandpa, the light's gone out!
[NARRATOR | slow, measured] Old Tom put down his pipe. He didn't hurry. He never hurried.
[TOM | gentle, steady] Then we'd best go and light it again.

[MIRA | whispering, afraid] In this?
[PAUSE 1.5s]
[TOM | warm, slightly amused] Ships don't wait for good weather, love.
```

## How the directions are read

The direction words are matched to one of Narrator's 13 feelings: neutral, happy, excited, amused, tender, sad, angry, fearful, surprised, tense, serious, sarcastic and whisper.

- *whispering*, *hushed*, *murmuring* or *muttering* always whisper.
- Otherwise the first feeling named wins: *scared, angry* is played scared.
- *very*, *extremely*, *shouting* or *screaming* make it stronger; *slightly*, *a little* or *quietly* make it softer.
- *calm* or *steady* on its own reads it plainly.

You can change any line's feeling and strength on the story's page before rendering.
