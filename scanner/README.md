# sdrscan

`sdrscan` is an unattended radio scanner for a Raspberry Pi 5 with an RTL-SDR Blog V4. It
sweeps the US band plan, finds and tracks transmissions, keeps short clips of speech under a
rolling storage quota, and summarizes the activity on the device. Once an hour it pushes a
digest of a few KB to an off-site static host. A phone-friendly dashboard reads that
digest, so looking at it costs the rig no data at all.

Design: [`docs/superpowers/specs/2026-11-03-sdr-scanner-design.md`](../docs/superpowers/specs/2026-11-03-sdr-scanner-design.md).
Plan: [`docs/superpowers/plans/2026-11-03-sdr-scanner.md`](../docs/superpowers/plans/2026-11-03-sdr-scanner.md).

## How it works

1. The scheduler sweeps the bands in 2.4 MHz windows and dwells on windows with active
   voice channels.
2. The detector finds peaks above each window's own noise floor. The tracker turns them into
   transmissions, keyed by integer Hz.
3. During a dwell, each channel is demodulated to 16 kHz audio. The voice gate classifies it
   as voice, data or noise, and measures the CTCSS tone (67 to 254.1 Hz) before it filters
   the tone out. A short carrier with no voice or data (5 s at most) is logged as a key-up.
   Every transmission keeps its tone, which names the repeater or user group. A voice
   transmission of at least 1.5 s keeps an 8 kbit/s Opus clip.
4. Speech-to-text (whisper.cpp `whisper-cli` with the `base.en` q5_1 model, built into the
   image) turns kept clips into short transcripts at nice 19, and pauses when the rig runs
   hot. Transcripts show on the rig's live page. They go into the off-site digest only with
   `uplink.include_transcripts: true`, because the off-site page is publicly readable.
   Call signs are read from each transcript (amateur, GMRS, and aircraft registrations on
   airband; written or spelled with phonetics) and follow the same setting.
5. Hour, day and all-time rollups feed a rule-based digest: busiest channels with key-ups,
   tones and call signs, the most heard call signs, new, unusual and quiet channels, bands,
   recent transmissions, what each pipeline stage did, and rig health. Each stage adds to
   an hourly counter in the database (survey hops, peaks, tracks opened, dwell time, voice,
   key-ups, clips kept and evicted, transcripts, call signs).
6. The digest goes off site once an hour, within a monthly byte cap.

## Try it on a build host

```bash
cd scanner
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q -m "not slow"

# Simulate two hours of a synthetic scene and look at the dashboard
.venv/bin/sdrscan simulate --scene tests/scenes/week_basic.yaml --hours 2 --out build/sim
python3 -m http.server --bind 127.0.0.1 --directory build/sim 8000
# open http://127.0.0.1:8000/ (use the browser's phone view at 360 px wide)
```

`--spectrum hybrid` also renders IQ for dwells, so the simulation produces clips and
transcripts (from a stub engine). On the x86 build host one simulated hour takes about
10 s with the default fast spectrum path.

Browser checks need Playwright (`pip install -e '.[browser]'` and
`python -m playwright install webkit`). Set `SDRSCAN_CHROME` to use a local Chrome.

## Testing a stage from a file

Each pipeline stage is also a command that reads one file and writes one. The commands call
the same code as the running service, so a stage can be checked on a recording without the
rest of the pipeline. Every JSON record carries a `schema` field such as
`sdrscan.detections/1`.

| Stage | Command | Reads | Writes |
|---|---|---|---|
| capture | `sdrscan capture` | the RTL-SDR | `.cu8` or `.cf32` and a JSON sidecar |
| detect | `sdrscan detect` | an IQ file | detections JSONL, one 100 ms hop per line |
| demod | `sdrscan demod` | an IQ file, `--freq`, optional `--mode` | 16 kHz mono WAV |
| gate | `sdrscan gate` | a WAV | segments JSONL (voice, data, noise) and a summary with `ctcss_hz` |
| stt | `sdrscan stt` | a WAV | transcript JSON |
| digest | `sdrscan digest --events` (or `--db`) | detections JSONL (or the database) | `digest.json` |
| dashboard | `sdrscan dashboard` | `digest.json.gz` | `index.html` |

IQ files can be rtl_sdr raw 8-bit (`.cu8`, `.raw`), `.cf32` or SigMF. A non-SigMF file needs
a JSON sidecar with the same stem that gives the sample rate and center frequency
(`rate`/`center_hz` or `sample_rate`/`center_freq_hz`); gain is optional. `--offset` and
`--seconds` pick a slice of a large file.

```bash
sdrscan capture --freq 443.15M --seconds 10 --gain 30 --out cap.cu8   # needs the dongle
sdrscan corpus cut --src big.cf32 --offset 0 --seconds 2 --out cap.cu8   # or slice a recording
sdrscan detect --iq cap.cu8 --out det.jsonl          # prints the channels heard
sdrscan demod --iq cap.cu8 --freq 443.15M --out ch.wav
sdrscan gate --wav ch.wav --out seg.jsonl            # prints the class: voice, data or unknown
sdrscan stt --wav ch.wav --binary whisper-cli --model ggml-base.en-q5_1.bin --out tr.json
sdrscan digest --events det.jsonl --out www && sdrscan dashboard --digest www/digest.json.gz --out www
```

`digest --events` replays the detections through the tracker as one dwell, so voice class
stays `unknown`; that needs the gate.

Real recordings stay out of the repository. `tests/corpus/manifest.json` lists clips by
their path relative to a capture directory, the slice, the sha256 of the slice's bytes and
the expected stage results. Set `SDRSCAN_IQ_CORPUS` to your capture directory to run them:

```bash
SDRSCAN_IQ_CORPUS=/path/to/captures .venv/bin/python -m pytest -q -m corpus
```

Without the variable these tests skip. A missing file or changed bytes skips that clip and
says which. To add a clip, run `sdrscan corpus cut` on it (it prints the source sha256),
run the stages, and write assertions that are robust: presence, absence, class and
frequency within a tolerance, not exact counts.

## Data budget

All figures are measured on the build host with the test fixtures, except the TLS
overhead, which is a configured estimate (6 KB per HTTPS request). The on-device ledger
measures the real numbers after deployment.

| Item | Bytes |
|---|---|
| Hourly digest, gzipped (week fixture) | about 1.4 KB (1.9 KB with transcripts and call signs), capped at 4 KB for 500 busy channels |
| One hourly push with headers and the TLS estimate | about 7.6 KB |
| Per day (24 pushes) | about 180 KB, about 5.5 MB per month |
| Dashboard page, gzipped, uploaded only when it changes | about 6.8 KB |
| Off-site view, first open | 0 rig bytes; the phone fetches about 6.8 KB page + 1 to 4 KB digest |
| Off-site view, later open | 0 rig bytes; about 0.3 KB on a 304, or the digest |
| Live page over the tailnet, first open | about 7.1 KB (estimate: the page plus headers), charged to the ledger |

The ledger's hard cap is 30 MB per month. Above 80% the digest goes every 3 h; at the cap
only a 1 KB health beat goes out per day and the live server answers 429.

## Deploying (not done yet)

The rig is not reachable now, so nothing has been installed. The install kit is in
`deploy/`:

1. Build the image: `docker buildx build --platform linux/arm64 --target runtime -t sdrscan:arm64 --load scanner/`.
2. Export it: `docker save sdrscan:arm64 | zstd -19 > sdrscan-arm64.tar.zst`.
3. Copy the tarball and `deploy/` to the rig.
4. Run `sudo deploy/install.sh --dry-run --image sdrscan-arm64.tar.zst`, read the output,
   then run it without `--dry-run`.
5. Fill in the S3 settings in `/etc/sdrscan/uplink.env` (mode 0600) and restart the
   service. Until then the scanner runs with pushes off and the digest health shows
   `uplink_unconfigured`.

The default off-site target is an S3-compatible bucket (`uplink.target: s3`). Use a
long-lived access key that can only `PutObject` under one prefix of one bucket; the rig
signs each request itself (SigV4), so nothing expires. Make that prefix publicly readable
and open `<endpoint>/<bucket>/<prefix>/index.html` on the phone. S3 keeps the
`Content-Encoding: gzip` the rig stores, so the page and digest travel gzipped. Signing
needs the rig clock within 15 minutes of real time. `uplink.env` keys:

| Key | Meaning |
| --- | --- |
| `SDRSCAN_S3_ENDPOINT` | `https://` origin of the object store, no bucket |
| `SDRSCAN_S3_BUCKET` | bucket name (path-style addressing) |
| `SDRSCAN_S3_PREFIX` | optional key prefix, for example the site name |
| `SDRSCAN_S3_REGION` | optional, default `us-east-1` |
| `SDRSCAN_S3_ACCESS_KEY`, `SDRSCAN_S3_SECRET_KEY` | the scoped key pair |

`http_put` (WebDAV with a fixed `SDRSCAN_UPLINK_AUTH` header) and `http_post` (a collector on
a tailnet peer) remain; they get the page uncompressed. An S3 presigned URL does not work as
`http_put`: it signs one object key and expires within 7 days. The S3 path is tested against
MinIO in Docker (`tests/test_uplink_s3.py`); it has not run against a hosted bucket yet.

`install.sh` checks the OS, disk, DVB driver blacklist and `rtl_test` sample loss first. It
keeps the previous image as `sdrscan:previous` and rolls back when the service is not healthy
within 120 s. `uninstall.sh` keeps the data volume unless `--purge` is given.

## Open measurements

These need the real rig:

1. Retune settle time per band.
2. Dropped samples at 2.4 MS/s on the Pi 5.
3. CPU per survey hop and per demodulated channel, with remote access in use
   (`sdrscan bench --json` prints these).
4. Gain per band, and the 10 dB floor threshold on real 8-bit captures.
5. Voice gate thresholds on real radio audio; they come from v1 and are untuned.
6. Speech-to-text real-time factor at 2 threads, and SoC temperature on a hot afternoon.
7. Ogg Opus clip playback in iOS Safari.
8. Speech-to-text speed and accuracy on real radio audio. Under qemu on the build host,
   `whisper-cli` transcribed the 11 s `jfk.wav` sample correctly in about 2 minutes; the Pi
   is expected to be 20 to 40 times faster than qemu.
9. CTCSS detection and key-up classification on real repeaters (one corpus clip reads
   107.2 Hz), and call-sign accuracy of `base.en` on real radio speech.
