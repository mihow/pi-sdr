# Captioned highlight video

This procedure turns a few archived scanner clips into a short video that you can
play on a phone and share. The video shows a title card, a scrolling voice-band
spectrogram, and burned-in captions timed to each word. The audio is cleaned.
Personal names are replaced with `[name]`. Call signs stay, because amateur and GMRS
transmissions are public and the call sign identifies the station, not the person.

The first use was a GMRS repeater net. The input was 6 transmissions from 87
archived clips, and the output was a 2 min 46 s, 11.5 MB MP4.

## What you need

- **A pulled clip archive.** It has an `index.json` from the scanner's clip store
  and the `.opus` clips it names. Each row needs `hz`, `t_start`, `dur_ms`, `path`
  and `transcript`. An optional `file` field gives a renamed local copy.
- **ffmpeg 6 or later** with libx264, aac, afftdn, showspectrum, drawtext and
  subtitles (libass). Also the **Noto Sans** fonts, found with `fc-match`.
- **A GPU host with faster-whisper** and a CTranslate2 Whisper model for the
  caption pass. `faster-whisper-large-v3-turbo` worked well. The Pi's whisper.cpp
  `base.en` transcripts help you choose clips, but they are too rough for captions.
- **A names file, kept out of git.** Put one personal name per line, and use `#`
  for comments. The scanner has no list of names, so you write it by reading the
  transcripts.

## Procedure

1. Pull the clip archive into a working folder outside this repo.
2. Read the transcripts in `index.json`. Choose the transmissions to use, and note
   each start time in local time (`HH:MM:SS`).
3. Copy `spec.example.json` to the working folder as `spec.json`. Set `hz`,
   `utc_offset_h`, `picks` and `titles`. Paths in the spec are relative to the
   spec file.
4. To use only the first part of a long transmission, add `"keep_s": N` to its
   pick.
5. Cut and clean the segments:
   `python3 make_highlight.py cut --spec spec.json`
6. Listen to `work/seg/*.wav`. If a segment is wrong, change its pick and repeat
   step 5.
7. Transcribe with word timestamps on the GPU host:
   `python transcribe_words.py MODEL_DIR work/seg work/words.json --prompt "GMRS net on the 550 repeater. Roll call, check-ins, net control."`
8. Read the text that `transcribe_words.py` prints. Add every personal name you see
   to the names file.
9. If the model gets a term wrong every time, add a regex fix to `caption_fixes`.
   For example, it heard "the 550 repeater" as "ST550".
10. Build the captions, the audio mix and the video:
    `python3 make_highlight.py post --spec spec.json`
11. Check one frame:
    `ffmpeg -ss 60 -i OUTPUT.mp4 -frames:v 1 frame.png`
    The spectrogram must fill its panel, and the captions must be readable.
12. Search `work/captions.srt` for names you missed. Then repeat step 10.
    Steps 10 to 12 take seconds and need no GPU.

## Choices behind the defaults

- **Cleaning chain.** The chain is `highpass 300 Hz, lowpass 3200 Hz, afftdn nr=15`.
  On a noisy repeater it removed about 10 dB of hiss from the pauses. It also gave
  the best mean transcription confidence of the four chains tried. Heavier chains
  (spectral gating, `speechnorm`) sounded worse and lowered the confidence.
- **Squelch tail.** The last 0.35 s of each clip is removed, because that is the
  noise burst when the station unkeys.
- **Spacing.** A 1.5 s lead-in plays under the title card, and 0.7 s of silence
  separates transmissions. The mix is loudness-normalized to -16 LUFS, a common
  level for phone playback.
- **Spectrogram range.** The audio goes to the spectrogram at 8 kHz, so the panel
  shows only 0–4 kHz. At 44.1 kHz, voice fills only the bottom fifth of the panel.
- **Caption cues.** A cue ends at a sentence end, or after 9 words, or after 3.6 s.
- **Phone playback.** The video is H.264 High profile level 4.0 with yuv420p, plus
  AAC at 96 kbit/s and `+faststart`, so iOS Safari and Photos play it at once.

## Known limits

- The spectrogram scrolls slowly (about 9 px/s), so syllables run together.
  A faster scroll or a per-transmission view would show the voice better.
- Name redaction works only from the names file. A name the model misspells gets
  through, so read the captions before you share the video.
- Word timing can move by a few tens of milliseconds between runs. The number of
  cues can also change by one or two.
