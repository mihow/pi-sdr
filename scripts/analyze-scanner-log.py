#!/usr/bin/env python3
"""Analyze a scanner docker log to produce per-channel signal statistics.

Input: `docker logs openwebrx-scanner` output (plain text, any length).
Usage: analyze-scanner-log.py <scanner.log> [<api-state.json>]

The scanner (feat/radio-scanner branch) emits one line per scanned channel per
scan pass. Each line looks like:

    22:09:28 INFO scanner.scanner: Signal on Marine Ch13 Bridge (156.650 MHz) S=32.4 dB

The binary signal/no-signal classification is broken (see project_scanner_bugs
memory: squelch scale mismatch fires on every channel), but the smeter values
are real FFT power computed from the RTL-SDR samples. That makes the per-line
S-meter a continuous measurement we can mine for per-channel noise floor,
dynamic range, and peak excursions.

Outputs a markdown report on stdout with:
  - Per-channel stats: count, noise floor (p10), median, ceiling (p90), max,
    dynamic range (p90 - p10), peak excursion (max - p10)
  - Top channels by peak excursion (most likely to contain real signals)
  - Per-band (group) aggregates with a noise floor recommendation
  - Time-of-day mean smeter for the top channels

If api-state.json is supplied, channels are labeled with their scanner group
(FM, HAM 2m, NOAA, ...). Otherwise group is derived from the channel name prefix.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LINE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}) INFO scanner\.scanner: "
    r"Signal on (.+?) \(([\d.]+) MHz\) S=([-\d.]+) dB"
)


def parse_log(path: Path):
    """Yield (hour, minute, second, channel, freq_mhz, smeter_db) tuples."""
    with path.open("r", errors="replace") as f:
        for line in f:
            m = LINE_RE.search(line)
            if not m:
                continue
            h, mn, sc, chan, freq, smeter = m.groups()
            yield int(h), int(mn), int(sc), chan, float(freq), float(smeter)


def percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def load_groups(state_path: Path) -> dict[float, str]:
    """Load channel groups keyed by frequency (Hz → MHz)."""
    with state_path.open() as f:
        state = json.load(f)
    return {
        round(ch["freq"] / 1_000_000, 4): ch.get("group", "unknown")
        for ch in state.get("channels", [])
    }


def analyze(log_path: Path):
    # Key by frequency (unique per channel), not name — the scanner config has
    # multiple channels sharing names like "2m Repeater" at different freqs.
    per_channel: dict[float, list[float]] = defaultdict(list)
    name_map: dict[float, str] = {}
    hourly: dict[float, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    total = 0

    for hr, _, _, chan, freq, sm in parse_log(log_path):
        per_channel[freq].append(sm)
        name_map[freq] = chan
        hourly[freq][hr].append(sm)
        total += 1

    return per_channel, name_map, hourly, total


def per_channel_stats(
    per_channel: dict[float, list[float]],
    name_map: dict[float, str],
    groups: dict[float, str],
) -> list[dict]:
    rows = []
    for freq, vals in per_channel.items():
        vs = sorted(vals)
        p10 = percentile(vs, 10)
        p50 = percentile(vs, 50)
        p90 = percentile(vs, 90)
        p99 = percentile(vs, 99)
        chan = name_map[freq]
        rows.append(
            {
                "channel": chan,
                "group": groups.get(freq, chan.split()[0] if chan else "unknown"),
                "freq": freq,
                "count": len(vals),
                "noise_floor": p10,
                "median": p50,
                "ceiling": p90,
                "p99": p99,
                "max": max(vals),
                "dynamic_range": p90 - p10,
                "peak_excursion": max(vals) - p10,
            }
        )
    return rows


def group_stats(rows: list[dict]) -> list[dict]:
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_group[r["group"]].append(r)

    out = []
    for group, chans in by_group.items():
        floors = sorted(r["noise_floor"] for r in chans)
        ceilings = sorted(r["ceiling"] for r in chans)
        out.append(
            {
                "group": group,
                "n_channels": len(chans),
                "noise_floor_p10": percentile(floors, 10),
                "noise_floor_median": percentile(floors, 50),
                "ceiling_median": percentile(ceilings, 50),
                "max_peak_exc": max(r["peak_excursion"] for r in chans),
            }
        )
    out.sort(key=lambda g: g["max_peak_exc"], reverse=True)
    return out


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    parts = ["| " + " | ".join(headers) + " |"]
    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        parts.append("| " + " | ".join(r) + " |")
    return "\n".join(parts)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    log_path = Path(sys.argv[1])
    state_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    print(f"Parsing {log_path}...", file=sys.stderr)
    per_channel, name_map, hourly, total = analyze(log_path)
    print(
        f"Parsed {total:,} signal events across {len(per_channel)} channels",
        file=sys.stderr,
    )

    groups: dict[float, str] = load_groups(state_path) if state_path else {}
    rows = per_channel_stats(per_channel, name_map, groups)

    print("# Scanner Signal Mining Report\n")
    print(f"**Input:** `{log_path}`  ")
    print(f"**Events parsed:** {total:,}  ")
    print(f"**Channels:** {len(per_channel)}  ")
    print()

    # Per-band summary
    print("## Per-band aggregates\n")
    print(
        "Noise floor = p10 of per-channel noise floors in group. Max peak excursion = "
        "biggest (max - p10) observed for any channel in the group. High peak "
        "excursion = at least one channel in this band saw real signal activity above "
        "background.\n"
    )
    g_rows = [
        [
            g["group"],
            str(g["n_channels"]),
            f"{g['noise_floor_p10']:.1f}",
            f"{g['noise_floor_median']:.1f}",
            f"{g['ceiling_median']:.1f}",
            f"{g['max_peak_exc']:.1f}",
        ]
        for g in group_stats(rows)
    ]
    print(
        md_table(
            ["group", "n", "floor p10", "floor med", "ceiling med", "max peak exc"],
            g_rows,
        )
    )
    print()

    # Top channels by peak excursion — most likely to contain real signals
    print("## Top 30 channels by peak excursion\n")
    print(
        "Peak excursion = max observed smeter − channel noise floor (p10). High values "
        "indicate the channel saw at least one bright transmission above its own "
        "background. Low values mean the channel's smeter was always near its floor "
        "— probably never carried traffic during the capture window.\n"
    )
    top = sorted(rows, key=lambda r: r["peak_excursion"], reverse=True)[:30]
    t_rows = [
        [
            r["channel"],
            r["group"],
            f"{r['freq']:.4f}",
            str(r["count"]),
            f"{r['noise_floor']:.1f}",
            f"{r['median']:.1f}",
            f"{r['ceiling']:.1f}",
            f"{r['max']:.1f}",
            f"{r['peak_excursion']:.1f}",
        ]
        for r in top
    ]
    print(
        md_table(
            [
                "channel",
                "group",
                "MHz",
                "n",
                "floor",
                "median",
                "p90",
                "max",
                "peak exc",
            ],
            t_rows,
        )
    )
    print()

    # Bottom channels by peak excursion — candidates for bookmark pruning
    print("## Bottom 20 channels by peak excursion\n")
    print(
        "These channels never saw smeter meaningfully above their floor during the "
        "capture. Candidates for bookmark pruning in the pi-sdr OpenWebRX+ default "
        "profiles — or candidates for keeping if you care about coverage regardless "
        "of activity.\n"
    )
    bot = sorted(rows, key=lambda r: r["peak_excursion"])[:20]
    b_rows = [
        [
            r["channel"],
            r["group"],
            f"{r['freq']:.4f}",
            str(r["count"]),
            f"{r['noise_floor']:.1f}",
            f"{r['ceiling']:.1f}",
            f"{r['peak_excursion']:.1f}",
        ]
        for r in bot
    ]
    print(
        md_table(
            ["channel", "group", "MHz", "n", "floor", "p90", "peak exc"], b_rows
        )
    )
    print()

    # Time-of-day activity for the top-10 channels
    print("## Hour-of-day mean smeter for top 10 channels\n")
    print(
        "Mean smeter (dB) grouped by hour of day (UTC). Identifies when each channel "
        "is brightest — useful for scheduling scans or understanding local traffic "
        "patterns.\n"
    )
    top10 = top[:10]
    hour_headers = ["channel", "MHz"] + [f"{h:02d}" for h in range(24)]
    h_rows = []
    for r in top10:
        row = [r["channel"], f"{r['freq']:.3f}"]
        for h in range(24):
            vals = hourly[r["freq"]].get(h, [])
            row.append(f"{sum(vals) / len(vals):.0f}" if vals else "-")
        h_rows.append(row)
    print(md_table(hour_headers, h_rows))
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
