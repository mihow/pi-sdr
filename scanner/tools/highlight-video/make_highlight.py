#!/usr/bin/env python3
"""Build a captioned highlight video from archived scanner clips.

The output is an MP4 (H.264 + AAC, faststart) that plays on a phone: a title card,
a scrolling voice-band spectrogram, and burned-in captions timed to each word.
Personal names listed in a separate, uncommitted file are replaced with "[name]".

The build has four stages. Each reads and writes files in the work directory, so
any stage can be re-run alone after an edit:

  cut       index.json + spec  -> work/seg/NN.wav   (trimmed, cleaned, 16 kHz mono)
  (words)   transcribe_words.py work/seg -> work/words.json   (GPU host, faster-whisper)
  captions  words.json + spec  -> work/captions.srt + work/timeline.json
  mix       seg/*.wav + timeline -> work/audio.wav  (lead-in, gaps, loudness-normalized)
  render    audio.wav + captions -> the MP4

Usage:
  make_highlight.py cut      --spec spec.json
  make_highlight.py captions --spec spec.json
  make_highlight.py mix      --spec spec.json
  make_highlight.py render   --spec spec.json
  make_highlight.py post     --spec spec.json    # captions + mix + render

See README.md in this directory for the full procedure and the spec format.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import pathlib
import re
import subprocess

# Voice-band cleanup. Measured on a noisy repeater net: about 10 dB less hiss in the
# pauses, and the best mean transcription confidence of the chains tried. Heavier
# chains (spectral gating, speechnorm) sounded worse and lowered confidence.
DEFAULT_CLEAN = "highpass=f=300,lowpass=f=3200,afftdn=nr=15:nf=-30:tn=1"
# Each transmission ends with a burst of squelch-tail noise when the station unkeys.
SQUELCH_TAIL_S = 0.35
SEG_RATE = 16000
LEAD_S = 1.5  # silence under the title card before the first transmission
GAP_S = 0.7  # silence between transmissions
# A caption cue ends at a sentence end, or after this many words or seconds.
CUE_MAX_WORDS = 9
CUE_MAX_S = 3.6
CUE_HOLD_S = 0.25  # keep a cue on screen briefly after its last word

BG = "0x0d1117"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def font(pattern: str) -> str:
    return subprocess.run(
        ["fc-match", "-f", "%{file}", pattern], check=True, capture_output=True, text=True
    ).stdout


def load_spec(path: str) -> dict:
    spec = json.loads(pathlib.Path(path).read_text())
    base = pathlib.Path(path).resolve().parent
    for key in ("archive", "work", "output", "names_file"):
        if spec.get(key):
            spec[key] = str((base / spec[key]).resolve())
    spec["work"] = spec.get("work") or str(base / "work")
    return spec


def local_hms(t_start: int, utc_offset_h: float) -> str:
    t = dt.datetime.fromtimestamp(t_start, dt.timezone(dt.timedelta(hours=utc_offset_h)))
    return t.strftime("%H:%M:%S")


def stage_cut(spec: dict) -> None:
    archive = pathlib.Path(spec["archive"])
    rows = json.loads((archive / "index.json").read_text())
    by_time = {
        local_hms(r["t_start"], spec["utc_offset_h"]): r for r in rows if r["hz"] == spec["hz"]
    }
    seg = pathlib.Path(spec["work"]) / "seg"
    seg.mkdir(parents=True, exist_ok=True)
    for old in seg.glob("*.wav"):
        old.unlink()
    chain = spec.get("clean", DEFAULT_CLEAN)
    for i, pick in enumerate(spec["picks"]):
        row = by_time.get(pick["at"])
        if row is None:
            raise SystemExit(f"no clip on {spec['hz']} Hz starting at {pick['at']}")
        keep = pick.get("keep_s") or row["dur_ms"] / 1000
        dur = keep - SQUELCH_TAIL_S
        out = seg / f"{i:02}-{pick['at'].replace(':', '')}.wav"
        # "path" is where the scanner stored the clip; a pulled archive may rename it
        # and record the new location in "file".
        src = archive / row["path"]
        if not src.exists() and row.get("file"):
            src = archive / row["file"]
        run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
             "-af", f"atrim=0:{dur:.2f},{chain}", "-ar", str(SEG_RATE), "-ac", "1", str(out)])
        print(f"{out.name}  {dur:5.1f} s  {row.get('transcript', '')[:70]}")


def redactor(spec: dict):
    names: list[str] = []
    if spec.get("names_file"):
        names = [n.strip() for n in pathlib.Path(spec["names_file"]).read_text().splitlines()
                 if n.strip() and not n.startswith("#")]
    name_re = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
    fixes = [(re.compile(f), t) for f, t in spec.get("caption_fixes", [])]

    def clean(text: str) -> str:
        for pat, rep in fixes:
            text = pat.sub(rep, text)
        if name_re:
            text = name_re.sub("[name]", text)
            # "[name] and [name]" or "[name], [name]" collapses to one marker.
            text = re.sub(r"(\[name\][ ,.]*(and|in)?[ ,.]*){2,}", "[name] ", text)
        return re.sub(r"\s+", " ", text).strip()

    return clean


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def stage_captions(spec: dict) -> None:
    work = pathlib.Path(spec["work"])
    segs = json.loads((work / "words.json").read_text())
    clean = redactor(spec)
    cues: list[list] = []
    t0 = LEAD_S
    for seg in segs:
        cur: list = []
        for a, b, w in seg["words"]:
            cur.append((a, b, w))
            if (len(cur) >= CUE_MAX_WORDS or cur[-1][1] - cur[0][0] > CUE_MAX_S
                    or w.rstrip().endswith((".", "?", "!"))):
                cues.append([t0 + cur[0][0], t0 + cur[-1][1] + CUE_HOLD_S,
                             clean("".join(x[2] for x in cur))])
                cur = []
        if cur:
            cues.append([t0 + cur[0][0], t0 + cur[-1][1] + CUE_HOLD_S,
                         clean("".join(x[2] for x in cur))])
        t0 += seg["dur"] + GAP_S
    for i in range(len(cues) - 1):
        cues[i][1] = min(cues[i][1], cues[i + 1][0] - 0.02)
    with open(work / "captions.srt", "w") as f:
        for i, (a, b, text) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n\n")
    (work / "timeline.json").write_text(json.dumps({"lead": LEAD_S, "gap": GAP_S, "total": t0}))
    print(f"{len(cues)} cues, {t0:.1f} s")


def stage_mix(spec: dict) -> None:
    work = pathlib.Path(spec["work"])
    files = sorted(glob.glob(str(work / "seg" / "*.wav")))
    inputs: list[str] = []
    parts = [f"anullsrc=r={SEG_RATE}:cl=mono,atrim=0:{LEAD_S}[lead]"]
    for i, f in enumerate(files):
        inputs += ["-i", f]
        parts.append(f"[{i}:a]apad=pad_dur={GAP_S}[s{i}]")
    graph = (";".join(parts) + ";[lead]" + "".join(f"[s{i}]" for i in range(len(files)))
             + f"concat=n={len(files) + 1}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=7,aresample=44100[out]")
    run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
         "-map", "[out]", "-c:a", "pcm_s16le", str(work / "audio.wav")])


def stage_render(spec: dict) -> None:
    work = pathlib.Path(spec["work"])
    titles = spec["titles"]
    for name, text in (("t1", titles["title"]), ("t2", titles["subtitle"]),
                       ("t3", titles.get("spectrum_label", "0 - 4 kHz  ·  cleaned voice band")),
                       ("t4", titles.get("card", "Highlights"))):
        # Text goes through files, so colons and quotes need no drawtext escaping.
        (work / f"{name}.txt").write_text(text)
    bold, regular = font("Noto Sans:bold"), font("Noto Sans")
    # Resampling to 8 kHz makes the spectrogram's height span 0-4 kHz, the voice band.
    graph = (
        "[0:a]aresample=8000,showspectrum=s=1280x360:mode=combined:slide=scroll:color=viridis:"
        "scale=log:fscale=lin:saturation=1.1:gain=1.2:legend=0,format=yuv420p[spec];"
        f"color=c={BG}:s=1280x720:r=30[bg];"
        "[bg][spec]overlay=0:150:shortest=1,"
        f"drawtext=fontfile={bold}:textfile=t1.txt:fontcolor=white:fontsize=40:x=48:y=40,"
        f"drawtext=fontfile={regular}:textfile=t2.txt:fontcolor=0x9aa4b2:fontsize=24:x=50:y=96,"
        f"drawtext=fontfile={regular}:textfile=t3.txt:fontcolor=0x6e7681:fontsize=18:x=50:y=520,"
        f"drawtext=fontfile={bold}:textfile=t4.txt:fontcolor=white:fontsize=34:x=(w-tw)/2:"
        f"y=(h-th)/2:box=1:boxcolor={BG}@0.85:boxborderw=24:enable=lt(t\\,1.4),"
        "subtitles=captions.srt:force_style='FontName=Noto Sans,FontSize=26,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00111111,BorderStyle=1,Outline=2,"
        "Shadow=0,Alignment=2,MarginV=42'[v]"
    )
    (work / "vf.txt").write_text(graph)
    out = spec["output"]
    # Relative textfile/subtitles paths resolve against the work directory.
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", "audio.wav", "-filter_complex_script", "vf.txt",
         "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
         "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "23", "-r", "30", "-c:a", "aac",
         "-b:a", "96k", "-ar", "44100", "-movflags", "+faststart", "-shortest", out],
        check=True, cwd=work)
    print(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["cut", "captions", "mix", "render", "post"])
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()
    spec = load_spec(args.spec)
    stages = {"cut": [stage_cut], "captions": [stage_captions], "mix": [stage_mix],
              "render": [stage_render], "post": [stage_captions, stage_mix, stage_render]}
    for fn in stages[args.stage]:
        fn(spec)


if __name__ == "__main__":
    main()
