#!/usr/bin/env python3
"""Transcribe cleaned highlight segments with word timestamps, for caption timing.

Runs on a GPU host with faster-whisper installed, not on the Pi. The Pi's own
transcripts (whisper.cpp base.en) are good enough to find a net, but captions need
the larger model and per-word times.

  transcribe_words.py MODEL_DIR SEG_DIR OUT.json [--prompt TEXT]

MODEL_DIR is a CTranslate2 model, for example faster-whisper-large-v3-turbo.
faster-whisper is given a numpy array, not a path, so it does not need ffmpeg's
Python bindings. VAD is off because each segment is already one transmission.
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import wave

import numpy as np
from faster_whisper import WhisperModel

DEFAULT_PROMPT = "GMRS net on the repeater. Roll call, check-ins, net control."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("seg_dir")
    ap.add_argument("out")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help="vocabulary hint: the repeater, call-sign style, net words")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()
    compute = "float16" if args.device == "cuda" else "int8"
    model = WhisperModel(args.model, device=args.device, compute_type=compute)
    out = []
    for f in sorted(glob.glob(str(pathlib.Path(args.seg_dir) / "*.wav"))):
        with wave.open(f) as w:
            rate, n = w.getframerate(), w.getnframes()
            audio = np.frombuffer(w.readframes(n), np.int16).astype(np.float32) / 32768
        segs, _ = model.transcribe(audio, beam_size=5, language="en", word_timestamps=True,
                                   vad_filter=False, initial_prompt=args.prompt)
        words = [(x.start, x.end, x.word) for s in segs for x in (s.words or [])]
        out.append({"file": pathlib.Path(f).name, "dur": n / rate, "words": words})
        print(pathlib.Path(f).name, "|", "".join(w[2] for w in words)[:100])
    pathlib.Path(args.out).write_text(json.dumps(out, indent=1))
    print(sum(len(o["words"]) for o in out), "words")


if __name__ == "__main__":
    main()
