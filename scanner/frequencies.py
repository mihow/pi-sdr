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
