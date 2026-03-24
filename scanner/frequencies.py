"""
Frequency database for scanner.
GMRS, FRS, HAM 2m/70cm, MURS, Marine, NOAA Weather.
All frequencies in Hz. Modulation types match OpenWebRX+ modes.
"""

GMRS_CHANNELS = [
    # GMRS/FRS shared channels 1-7 (simplex, 0.5W FRS / 5W GMRS)
    {"freq": 462_562_500, "name": "GMRS 1", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_587_500, "name": "GMRS 2", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_612_500, "name": "GMRS 3", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_637_500, "name": "GMRS 4", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_662_500, "name": "GMRS 5", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_687_500, "name": "GMRS 6", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    {"freq": 462_712_500, "name": "GMRS 7", "mod": "nfm", "group": "GMRS", "bandwidth": 12500},
    # GMRS repeater channels 15R-22R (output freq, 50W)
    {"freq": 462_550_000, "name": "GMRS 15R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_575_000, "name": "GMRS 16R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_600_000, "name": "GMRS 17R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_625_000, "name": "GMRS 18R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_650_000, "name": "GMRS 19R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_675_000, "name": "GMRS 20R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_700_000, "name": "GMRS 21R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
    {"freq": 462_725_000, "name": "GMRS 22R", "mod": "nfm", "group": "GMRS", "bandwidth": 25000},
]

FRS_EXTRA_CHANNELS = [
    # FRS-only channels 8-14 (462 MHz, 0.5W)
    {"freq": 467_562_500, "name": "FRS 8", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_587_500, "name": "FRS 9", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_612_500, "name": "FRS 10", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_637_500, "name": "FRS 11", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_662_500, "name": "FRS 12", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_687_500, "name": "FRS 13", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
    {"freq": 467_712_500, "name": "FRS 14", "mod": "nfm", "group": "FRS", "bandwidth": 12500},
]

HAM_2M = [
    {"freq": 146_520_000, "name": "2m Simplex Call", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_550_000, "name": "2m Simplex", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_580_000, "name": "2m Simplex", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_000_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_060_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_120_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_180_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_240_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_300_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 147_360_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_610_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_670_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_730_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_790_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_850_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_910_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
    {"freq": 146_970_000, "name": "2m Repeater", "mod": "nfm", "group": "HAM 2m", "bandwidth": 12500},
]

HAM_70CM = [
    {"freq": 446_000_000, "name": "70cm Simplex Call", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 446_500_000, "name": "70cm Simplex", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 447_000_000, "name": "70cm Repeater", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 447_060_000, "name": "70cm Repeater", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 447_120_000, "name": "70cm Repeater", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 449_000_000, "name": "70cm Repeater", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
    {"freq": 449_500_000, "name": "70cm Repeater", "mod": "nfm", "group": "HAM 70cm", "bandwidth": 12500},
]

MURS = [
    {"freq": 151_820_000, "name": "MURS 1", "mod": "nfm", "group": "MURS", "bandwidth": 11250},
    {"freq": 151_880_000, "name": "MURS 2", "mod": "nfm", "group": "MURS", "bandwidth": 11250},
    {"freq": 151_940_000, "name": "MURS 3", "mod": "nfm", "group": "MURS", "bandwidth": 11250},
    {"freq": 154_570_000, "name": "MURS 4", "mod": "nfm", "group": "MURS", "bandwidth": 20000},
    {"freq": 154_600_000, "name": "MURS 5", "mod": "nfm", "group": "MURS", "bandwidth": 20000},
]

MARINE = [
    {"freq": 156_800_000, "name": "Marine Ch16 Distress", "mod": "nfm", "group": "Marine", "bandwidth": 25000},
    {"freq": 156_450_000, "name": "Marine Ch9 Calling", "mod": "nfm", "group": "Marine", "bandwidth": 25000},
    {"freq": 156_650_000, "name": "Marine Ch13 Bridge", "mod": "nfm", "group": "Marine", "bandwidth": 25000},
]

NOAA_WEATHER = [
    {"freq": 162_400_000, "name": "NOAA WX1", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_425_000, "name": "NOAA WX2", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_450_000, "name": "NOAA WX3", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_475_000, "name": "NOAA WX4", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_500_000, "name": "NOAA WX5", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_525_000, "name": "NOAA WX6", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
    {"freq": 162_550_000, "name": "NOAA WX7", "mod": "nfm", "group": "NOAA", "bandwidth": 25000},
]

# Default scan list: GMRS first (most likely voice), then HAM, then others
ALL_CHANNELS = GMRS_CHANNELS + FRS_EXTRA_CHANNELS + HAM_2M + HAM_70CM + MURS + MARINE + NOAA_WEATHER


def get_default_scan_list() -> list[dict]:
    """Return the default frequency scan list."""
    return [ch.copy() for ch in ALL_CHANNELS]


def get_channels_by_group(group: str) -> list[dict]:
    """Return channels for a specific group."""
    return [ch.copy() for ch in ALL_CHANNELS if ch["group"] == group]


def get_groups() -> list[str]:
    """Return list of available frequency groups."""
    seen = []
    for ch in ALL_CHANNELS:
        if ch["group"] not in seen:
            seen.append(ch["group"])
    return seen


# --- Band windows for wideband FFT scanning ---

# Groups that should be scanned together in a single SDR window when possible.
# Each entry maps a logical band name to the channel groups it contains.
BAND_GROUPS = [
    {"name": "GMRS", "groups": ["GMRS"]},
    {"name": "FRS", "groups": ["FRS"]},
    {"name": "HAM 2m", "groups": ["HAM 2m"]},
    {"name": "HAM 70cm", "groups": ["HAM 70cm"]},
    {"name": "MURS", "groups": ["MURS"]},
    {"name": "Marine", "groups": ["Marine"]},
    {"name": "NOAA", "groups": ["NOAA"]},
]


# Pre-computed band windows for the default RTL-SDR 2.4 MHz bandwidth.
# Use get_band_windows() to recompute for different SDR bandwidths.
BAND_WINDOWS = []  # populated at module load, see bottom of file


def get_band_windows(max_bandwidth: int = 2_400_000) -> list[dict]:
    """Compute band windows sized for the given SDR bandwidth.

    Each window is a dict with:
        - name: human-readable band name
        - center: center frequency in Hz
        - groups: list of channel group names included
        - channels: list of channel dicts in this window

    Channels within a group are split across multiple windows if they
    span more than max_bandwidth. The center frequency is chosen so all
    channels fit within ±(max_bandwidth / 2).

    Args:
        max_bandwidth: Maximum SDR sample rate / bandwidth in Hz.
            2_400_000 for RTL-SDR, 10_000_000 for SDRplay.

    Returns:
        List of band window dicts, ordered by center frequency.
    """
    half_bw = max_bandwidth / 2
    windows: list[dict] = []

    for band in BAND_GROUPS:
        # Collect all channels for this band's groups
        channels = []
        for group_name in band["groups"]:
            channels.extend(get_channels_by_group(group_name))

        if not channels:
            continue

        # Sort by frequency
        channels.sort(key=lambda ch: ch["freq"])

        # Split into windows that fit within max_bandwidth
        # Greedy: start a new window when the next channel won't fit
        current_channels: list[dict] = [channels[0]]

        for ch in channels[1:]:
            span_low = current_channels[0]["freq"]
            span_high = ch["freq"]
            # Account for channel bandwidth on the edges
            total_span = (
                (span_high + current_channels[-1].get("bandwidth", 12500) / 2)
                - (span_low - current_channels[0].get("bandwidth", 12500) / 2)
            )
            # Recalculate with the new channel
            total_span_new = (
                (ch["freq"] + ch.get("bandwidth", 12500) / 2)
                - (span_low - current_channels[0].get("bandwidth", 12500) / 2)
            )

            if total_span_new <= max_bandwidth:
                current_channels.append(ch)
            else:
                # Emit current window and start a new one
                windows.append(_make_window(band["name"], current_channels))
                current_channels = [ch]

        # Emit the last window
        if current_channels:
            windows.append(_make_window(band["name"], current_channels))

    # Sort all windows by center frequency
    windows.sort(key=lambda w: w["center"])
    return windows


def _make_window(band_name: str, channels: list[dict]) -> dict:
    """Create a band window dict from a list of channels.

    Center frequency is the midpoint between the lowest and highest
    channel frequencies (accounting for edge channel bandwidths).
    """
    freqs = [ch["freq"] for ch in channels]
    min_freq = min(freqs) - channels[0].get("bandwidth", 12500) / 2
    max_freq = max(freqs) + channels[-1].get("bandwidth", 12500) / 2
    center = int((min_freq + max_freq) / 2)

    groups = list(dict.fromkeys(ch["group"] for ch in channels))

    # Name includes a suffix if the band is split across multiple windows
    name = band_name

    return {
        "name": name,
        "center": center,
        "groups": groups,
        "channels": [ch.copy() for ch in channels],
    }


# Populate BAND_WINDOWS at module load with default RTL-SDR bandwidth
BAND_WINDOWS.extend(get_band_windows())
