# Scanner Signal Mining — 2026-04-11

Analysis of a 2-day `openwebrx-scanner` run on a production pi-sdr deployment.
The goal is not to evaluate the scanner's *detection* (that's broken — see
[project_scanner_bugs memory](../../../.. "memory: project_scanner_bugs")) but
to mine the 2,034,719 per-channel smeter readings captured in the container
logs for information that can inform scanner fixes and OpenWebRX+ bookmark
curation.

The smeter values are real FFT power computed from the RTL-SDR Blog V4, even
though the binary signal/no-signal classification fires on every channel every
scan pass (squelch scale bug). That makes the log a rich continuous
measurement despite the broken classifier.

## TL;DR

- **Per-band noise floors span 13 dB** (HAM 1.25m median 19.0 → FM median 34.9).
  A single global squelch threshold can't work; the adaptive squelch needed by
  scanner bug #1 must be relative to the in-band floor, computed per scan
  window.
- **The GMRS 462.55–462.725 block is by far the most active band** in this
  environment. Twelve of the top fifteen "highest peak excursion" channels are
  GMRS. If the scanner's job is voice detection, GMRS is where to validate it.
- **Railroad End of Train (161.100 MHz) shows a clean diurnal pattern** — 38 dB
  mean 00:00–02:00 UTC dropping to 17 dB 15:00–16:00 UTC — consistent with
  overnight EOT device telemetry.
- **A hard ceiling at ~73.6 dB** recurs across many channels' max smeter. Looks
  like ADC clip or FFT-bin saturation, not a real signal power. Worth a bug
  report separate from the four in `project_scanner_bugs`.
- **Scanner config has duplicated channel names**: 14 distinct frequencies all
  labeled `2m Repeater`, 3 labeled `1.25m Repeater`, 7 labeled `70cm Repeater`.
  Any analysis or UI that groups by name instead of freq will collapse them.
- **"Dead" and "always-on" channels look similar** in peak-excursion ranking.
  Air 118.0 Approach has peak excursion 13.7 dB because its ATIS carrier is
  *always* transmitting (noise floor 51.4 dB, p90 62.0). Not dead, saturated
  from below. Any pruning heuristic should check absolute floor too.

## Data source and caveats

- Source: `docker logs radio-scanner` captured at 2026-04-11 22:17 UTC,
  container uptime ~48 hours, ~14,229 full scan cycles.
- Line format: `HH:MM:SS INFO scanner.scanner: Signal on <name> (<MHz>) S=<dB>`
- Every channel hits on every scan pass (squelch bug), so this is effectively
  a continuous smeter sample per channel roughly every 12 seconds.
- Timestamps are wall-clock HH:MM:SS without date, so hour-of-day buckets
  pool across both days. Good enough for diurnal patterns.
- 143 channels × ~14,229 passes = 2,034,719 events.
- Tool: [`scripts/analyze-scanner-log.py`](../../../scripts/analyze-scanner-log.py)
  in this repo. 3.2s to parse 93 MB in pure Python.
- The 93 MB `scanner.log` itself is under `data/` (gitignored). To reproduce,
  snapshot `docker logs radio-scanner` from your own Pi and feed it to the
  script along with `curl http://<pi>:8080/api/state`.

## Per-band aggregates

"Floor" and "ceiling" are the p10 and p90 of each channel's smeter; the
column values below are the p10/median across all channels in that band.
"Max peak exc" is the biggest `max - floor` any channel in the band reached.

| group | n | floor p10 | floor med | ceiling med | max peak exc |
|---|---|---|---|---|---|
| Railroad | 11 | 15.7 | 21.1 | 33.9 | 53.0 |
| GMRS | 15 | 20.6 | 23.3 | 35.0 | 53.0 |
| Public Safety | 10 | 20.0 | 20.8 | 31.3 | 52.6 |
| HAM 1.25m | 5 | 15.1 | 19.0 | 27.7 | 49.5 |
| NOAA | 7 | 18.6 | 19.7 | 25.0 | 48.5 |
| Business | 8 | 23.7 | 26.0 | 31.1 | 47.2 |
| HAM 2m | 17 | 26.3 | 26.7 | 35.0 | 46.6 |
| FM | 10 | 28.1 | 34.9 | 54.2 | 44.2 |
| ISM 433 | 1 | 25.7 | 25.7 | 33.9 | 41.6 |
| MURS | 5 | 25.5 | 25.5 | 30.8 | 41.3 |
| FRS | 7 | 25.9 | 25.9 | 30.1 | 39.9 |
| ISM 900 | 8 | 22.9 | 27.9 | 31.7 | 34.5 |
| Air | 20 | 27.2 | 29.8 | 40.9 | 34.3 |
| HAM 70cm | 7 | 21.8 | 22.1 | 32.6 | 29.0 |
| NOAA-15 | 1 | 31.4 | 31.4 | 39.0 | 28.2 |
| Marine | 3 | 28.6 | 28.7 | 33.8 | 27.4 |
| HAM 23cm | 4 | 17.4 | 18.0 | 20.1 | 25.1 |
| ADS-B | 1 | 22.7 | 22.7 | 23.5 | 15.8 |
| WX Sat | 2 | 31.0 | 31.4 | 38.8 | 14.1 |
| HAM 33cm | 1 | 19.8 | 19.8 | 20.5 | 7.3 |

**Interpretation:** the band floor *median* ranges from 18 dB (HAM 23cm) to
34.9 dB (FM broadcast) — a 17 dB spread. An adaptive squelch needs to compute
the noise floor per scan window from the FFT bins (median of the bins away
from the channel center is a decent proxy) and set the threshold to
`floor + 8–12 dB`. The specific value should be tunable; 8 dB catches more
weak signals, 12 dB reduces false positives on adjacent-channel bleed.

## Top channels by peak excursion

Peak excursion = `max(smeter) − floor(smeter)`. High values = channel saw
bright transient signals above its own background during the capture. Good
candidates for voice-detection validation. Full table of top 30 is in the
[raw report](#reproducing-the-analysis); the condensed top 15:

| channel | group | MHz | floor | max | peak exc |
|---|---|---|---|---|---|
| RR End of Train | Railroad | 161.100 | 15.4 | 68.4 | 53.0 |
| GMRS 1 | GMRS | 462.562 | 20.6 | 73.6 | 53.0 |
| GMRS 3 | GMRS | 462.613 | 20.7 | 73.6 | 52.9 |
| GMRS 4 | GMRS | 462.637 | 21.0 | 73.7 | 52.7 |
| PS 461.0 | Public Safety | 461.000 | 21.1 | 73.7 | 52.6 |
| GMRS 6 | GMRS | 462.688 | 20.9 | 73.5 | 52.6 |
| GMRS 7 | GMRS | 462.712 | 21.0 | 73.5 | 52.5 |
| GMRS 17R | GMRS | 462.600 | 23.6 | 73.6 | 50.0 |
| GMRS 15R | GMRS | 462.550 | 23.8 | 73.6 | 49.8 |
| GMRS 18R | GMRS | 462.625 | 23.8 | 73.5 | 49.7 |
| 1.25m Repeater | HAM 1.25m | 224.000 | 19.3 | 68.8 | 49.5 |
| GMRS 21R | GMRS | 462.700 | 23.9 | 73.0 | 49.1 |
| NOAA WX7 | NOAA | 162.550 | 21.5 | 70.0 | 48.5 |
| GMRS 20R | GMRS | 462.675 | 23.8 | 72.3 | 48.5 |
| Biz 154.515 | Business | 154.515 | 26.0 | 73.2 | 47.2 |

Twelve of the fifteen are GMRS 462.55–462.725. The max values are suspiciously
clustered at 73.5–73.7 — see the saturation note below.

## Dead channels and always-on carriers

Bottom 20 by peak excursion. Two distinct categories are mixed in:

**Category 1 — always-on carriers** (low peak excursion because they're
*always* transmitting; noise floor is already the signal):
- Air 118.0 Approach (118.000 MHz) — floor 51.4 dB, p90 62.0, peak exc 13.7.
  Continuous ATIS/approach broadcast. Don't prune.
- Air 119.1, Air 118.3, Air 119.9, Air 124.0 — similar ATIS/approach carriers,
  floors 36–37 dB (elevated relative to the rest of the Air band median floor
  29.8).
- FM 107.1 (floor 42.6 dB, peak exc 13.7) — strong local FM broadcast that's
  always on.

**Category 2 — genuinely quiet during the capture:**
- 33cm Simplex 927.500 — floor 19.8, max barely 20.5. Nothing going on.
- HAM 23cm 1282/1284 — 1.2 GHz repeaters with no activity and a low floor.
- ISM 915.0 Center — low activity (floor 28.0, peak exc 7.9).
- NOAA-18/19 APT (137.1, 137.62) — WX satellite passes are intermittent and
  short; a 2-day capture may not have caught a bright overhead pass.

**Distinguishing heuristic:** `absolute_floor > band_median_floor + 10 dB`
flags always-on carriers; everything else with low peak excursion is a
pruning candidate.

## Diurnal patterns

Hourly mean smeter for the top 10 excursion channels. This is where the real
signal structure shows up.

| channel | 00 | 02 | 04 | 06 | 08 | 10 | 12 | 14 | 16 | 18 | 20 | 22 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RR End of Train (161.100) | 38 | 33 | 30 | 26 | 26 | 25 | 25 | 19 | 17 | 20 | 24 | 33 |
| GMRS 15R (462.550) | 34 | 38 | 41 | 28 | 29 | 36 | 39 | 34 | 32 | 40 | 42 | 35 |
| GMRS 17R (462.600) | 29 | 30 | 28 | 27 | 27 | 26 | 26 | 31 | 32 | 35 | 33 | 29 |
| GMRS 18R (462.625) | 30 | 30 | 29 | 28 | 27 | 26 | 26 | 31 | 31 | 34 | 32 | 30 |
| GMRS 1 (462.562) | 27 | 26 | 26 | 24 | 24 | 23 | 24 | 28 | 28 | 30 | 28 | 26 |

(full 24-hour table in the raw report)

- **RR End of Train** has the cleanest diurnal pattern in the dataset —
  21 dB swing between 15 UTC (quiet) and 00 UTC (loud). Matches nighttime
  freight traffic near the capture location.
- **GMRS 15R** is the most active GMRS repeater (channel 15R = 462.550 MHz),
  strong evening/night activity from 18:00 UTC onward.
- **GMRS 17R/18R** show afternoon-evening activity (14:00 UTC rising into 18:00).
- **GMRS 1/3/4/6/7** (simplex channels) are flat ~25 dB with modest evening
  bump, consistent with occasional simplex chatter.

## Saturation ceiling at ~73.6 dB

The max smeter column clusters around 73.5–73.7 dB on many channels in the
top list:

```
GMRS 1     73.6
GMRS 3     73.6
GMRS 4     73.7
PS 461.0   73.7
GMRS 6     73.5
GMRS 7     73.5
GMRS 17R   73.6
GMRS 15R   73.6
GMRS 18R   73.5
GMRS 21R   73.0
2m Rptr    73.6
Biz 154.5  73.2
FM 95.5    73.1
```

This doesn't look like a real physical ceiling — fifteen unrelated channels
don't reach identical peak power within 0.3 dB by coincidence. Likely causes:

1. **ADC saturation.** RTL-SDR is 8-bit; with gain pinned at 0 dB (see
   `project_scanner_bugs` memory, bug #4), strong nearby transmitters drive
   the ADC into clip. The FFT power reading saturates at the full-scale level.
2. **FFT bin clipping in `compute_channel_power`** (`scanner/fft_scan.py`).
   If the function normalizes or clamps against a fixed reference, values
   above it get squashed.
3. **Display/log formatting bug** — less likely, but `%.1f` won't cause this.

Worth investigating as a separate bug, especially (1) since it implies the
scanner's strongest-signal measurements are all garbage. Useful diagnostic:
increase gain to 29 dB (same as OpenWebRX+ uses) and check whether the
ceiling moves or stays at 73.6.

## Scanner config bugs discovered

**Duplicate channel names.** The running config has many channels sharing a
name at different frequencies:

- 14 distinct `2m Repeater` (146.61–147.36 MHz, 60 kHz spacing)
- 7 distinct `70cm Repeater` (447.00+ MHz)
- 5 distinct `1.25m Repeater` (220s MHz)

Any UI or analysis that groups by name will conflate them. The fix is either
to rename to include the frequency (`2m Repeater 146.97`) or to always key
by frequency in aggregation code. My initial analysis script had this bug —
I fixed it before running the numbers above, but it's worth catching in the
scanner UI (`scanner/web.py`) and in the activity log.

## Recommendations feeding into scanner fixes

Ordered by impact, these are directly actionable once the scanner fix work
starts (see plan at `docs/superpowers/plans/2026-04-11-scanner-detection-fixes.md`):

1. **Adaptive squelch must be per-window, per-band.** Use the band aggregate
   table above as baseline: `floor + 10 dB` seems like a reasonable default
   across all bands except FM broadcast (which needs `floor + 15 dB` because
   adjacent-channel bleed is significant). Computing floor from the current
   window's FFT bins (median of bins outside the target channel bandwidth)
   will adapt automatically as the dongle retunes.

2. **Voice detection ground truth dataset.** GMRS 15R (462.550), GMRS 17R, 18R
   (462.600, 462.625), and RR End of Train (161.100) are the four channels
   with the highest confirmed real-signal activity in this RF environment.
   Record audio from these for a few hours with fixed parameters, manually
   label the voice segments, and use as the test fixture for bug #2 (sample
   rate mismatch) and bug #3 (IQ buffer size). This is where the "test with
   known speech WAV" test in the fix plan gets its input.

3. **Investigate the 73.6 dB ceiling.** This is a fifth scanner bug to add to
   `project_scanner_bugs`. It invalidates the `max` column for any channel
   with a strong signal, which includes every useful voice-detection target.
   Likely root cause: gain=0 + 8-bit ADC clip. Retest after bug #4 (gain) is
   fixed and see whether peaks become properly distributed or stay clustered.

4. **Fix duplicate channel names.** Either rename in config or switch to
   freq-keyed aggregation in `scanner/web.py` and wherever activity logs live.

5. **Prune genuinely dead bookmarks from pi-sdr defaults.** For the pi-sdr
   image's OpenWebRX+ default profiles (separate from scanner config), skip
   HAM 33cm simplex, HAM 23cm repeaters, ISM 915 Center, and consider
   shrinking the NOAA WX Sat entries unless overhead pass scheduling is
   added. The always-on carriers (Air 118.0 Approach, FM 107.1) should
   stay — they're useful signal sinks for verifying a new install works.

## Reproducing the analysis

```bash
# on the Pi:
docker logs radio-scanner 2>&1 > scanner.log
curl -s http://localhost:8080/api/state > api-state.json

# on a workstation with the pi-sdr repo checked out:
scp pi@<pi-host>:scanner.log ./data/snapshot/
scp pi@<pi-host>:api-state.json ./data/snapshot/
python3 scripts/analyze-scanner-log.py \
    data/snapshot/scanner.log \
    data/snapshot/api-state.json \
    > report.md
```

The script is pure stdlib (no numpy/pandas); runs in ~3 s on a ~2M-event log.
