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

# Aviation Air Band (AM modulation, 25 kHz spacing)
AIR_BAND = [
    {"freq": 118_000_000, "name": "Air 118.0 Approach", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 118_300_000, "name": "Air 118.3", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 118_700_000, "name": "Air 118.7", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 119_100_000, "name": "Air 119.1", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 119_500_000, "name": "Air 119.5", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 119_900_000, "name": "Air 119.9", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 120_500_000, "name": "Air 120.5 Tower", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 121_500_000, "name": "Air Guard/Emergency", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 121_900_000, "name": "Air 121.9 Ground", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 122_750_000, "name": "Air 122.75 Unicom", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 123_000_000, "name": "Air 123.0 Unicom", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 123_450_000, "name": "Air 123.45 Air-Air", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 124_000_000, "name": "Air 124.0", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 125_000_000, "name": "Air 125.0", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 126_200_000, "name": "Air 126.2 ATIS", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 127_000_000, "name": "Air 127.0 Center", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 128_000_000, "name": "Air 128.0", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 132_000_000, "name": "Air 132.0", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 134_000_000, "name": "Air 134.0", "mod": "am", "group": "Air", "bandwidth": 25000},
    {"freq": 135_000_000, "name": "Air 135.0", "mod": "am", "group": "Air", "bandwidth": 25000},
]

# Railroad (AAR channels, NFM)
RAILROAD = [
    {"freq": 160_215_000, "name": "RR Ch 1 Road", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_245_000, "name": "RR Ch 2 Road", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_320_000, "name": "RR Ch 3 Road", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_350_000, "name": "RR Ch 4 Road", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_380_000, "name": "RR Ch 5 Road", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_410_000, "name": "RR Ch 6 Yard", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_440_000, "name": "RR Ch 7 Yard", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 160_470_000, "name": "RR Ch 8 Yard", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 161_100_000, "name": "RR End of Train", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 161_550_000, "name": "RR Dispatcher", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
    {"freq": 161_565_000, "name": "RR Police", "mod": "nfm", "group": "Railroad", "bandwidth": 12500},
]

# FM Broadcast (WFM — wideband FM, 200 kHz bandwidth)
# Just a few common frequencies to test; user can add more
FM_BROADCAST = [
    {"freq":  88_500_000, "name": "FM 88.5", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq":  91_100_000, "name": "FM 91.1", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq":  93_100_000, "name": "FM 93.1", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq":  95_500_000, "name": "FM 95.5", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq":  97_300_000, "name": "FM 97.3", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq":  99_700_000, "name": "FM 99.7", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq": 101_300_000, "name": "FM 101.3", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq": 103_500_000, "name": "FM 103.5", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq": 105_100_000, "name": "FM 105.1", "mod": "wfm", "group": "FM", "bandwidth": 200000},
    {"freq": 107_100_000, "name": "FM 107.1", "mod": "wfm", "group": "FM", "bandwidth": 200000},
]

# NOAA Weather Satellites (APT, 137 MHz)
WX_SATELLITES = [
    {"freq": 137_100_000, "name": "NOAA-19 APT", "mod": "nfm", "group": "WX Sat", "bandwidth": 40000},
    {"freq": 137_620_000, "name": "NOAA-18 APT", "mod": "nfm", "group": "WX Sat", "bandwidth": 40000},
    {"freq": 137_912_500, "name": "NOAA-15 APT", "mod": "nfm", "group": "WX Sat", "bandwidth": 40000},
]

# ISM / IoT bands
ISM_433 = [
    {"freq": 433_920_000, "name": "ISM 433.92", "mod": "nfm", "group": "ISM 433", "bandwidth": 25000},
]

# HAM 23cm band (1240-1300 MHz)
HAM_23CM = [
    {"freq": 1_294_500_000, "name": "23cm Simplex Call", "mod": "nfm", "group": "HAM 23cm", "bandwidth": 12500},
    {"freq": 1_282_000_000, "name": "23cm Repeater", "mod": "nfm", "group": "HAM 23cm", "bandwidth": 12500},
    {"freq": 1_284_000_000, "name": "23cm Repeater", "mod": "nfm", "group": "HAM 23cm", "bandwidth": 12500},
    {"freq": 1_286_000_000, "name": "23cm Repeater", "mod": "nfm", "group": "HAM 23cm", "bandwidth": 12500},
]

# ISM 900 MHz band (902-928 MHz) — LoRa, smart meters, misc
ISM_900 = [
    {"freq": 902_000_000, "name": "ISM 902.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 906_000_000, "name": "ISM 906.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 910_000_000, "name": "ISM 910.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 914_000_000, "name": "ISM 914.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 915_000_000, "name": "ISM 915.0 Center", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 918_000_000, "name": "ISM 918.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 922_000_000, "name": "ISM 922.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
    {"freq": 926_000_000, "name": "ISM 926.0", "mod": "nfm", "group": "ISM 900", "bandwidth": 25000},
]

# ADS-B (1090 MHz) — aircraft transponders (not demodulable as NFM, but can detect signal presence)
ADSB = [
    {"freq": 1_090_000_000, "name": "ADS-B 1090", "mod": "nfm", "group": "ADS-B", "bandwidth": 25000},
]

# Public Safety UHF (common simplex/repeater, varies by area)
PUBLIC_SAFETY = [
    {"freq": 453_000_000, "name": "PS 453.0", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 453_500_000, "name": "PS 453.5", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 453_900_000, "name": "PS 453.9", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 454_000_000, "name": "PS 454.0", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 460_000_000, "name": "PS 460.0", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 460_500_000, "name": "PS 460.5", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 460_625_000, "name": "PS 460.625", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 461_000_000, "name": "PS 461.0", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 461_500_000, "name": "PS 461.5", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
    {"freq": 462_000_000, "name": "PS 462.0", "mod": "nfm", "group": "Public Safety", "bandwidth": 12500},
]

# Business / Industrial band (VHF 150-174 MHz)
BUSINESS_VHF = [
    {"freq": 151_625_000, "name": "Biz 151.625", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 151_955_000, "name": "Biz 151.955", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 152_240_000, "name": "Biz 152.240", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 154_515_000, "name": "Biz 154.515", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 154_540_000, "name": "Biz 154.540", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 158_400_000, "name": "Biz 158.400", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 159_000_000, "name": "Biz 159.0", "mod": "nfm", "group": "Business", "bandwidth": 12500},
    {"freq": 173_225_000, "name": "Biz 173.225 Itinerant", "mod": "nfm", "group": "Business", "bandwidth": 12500},
]

# HAM 33cm band (902-928 MHz, shared with ISM)
HAM_33CM = [
    {"freq": 927_500_000, "name": "33cm Simplex", "mod": "nfm", "group": "HAM 33cm", "bandwidth": 12500},
]

# HAM 1.25m band (222-225 MHz)
HAM_125M = [
    {"freq": 223_500_000, "name": "1.25m Simplex Call", "mod": "nfm", "group": "HAM 1.25m", "bandwidth": 12500},
    {"freq": 223_520_000, "name": "1.25m Simplex", "mod": "nfm", "group": "HAM 1.25m", "bandwidth": 12500},
    {"freq": 224_000_000, "name": "1.25m Repeater", "mod": "nfm", "group": "HAM 1.25m", "bandwidth": 12500},
    {"freq": 224_400_000, "name": "1.25m Repeater", "mod": "nfm", "group": "HAM 1.25m", "bandwidth": 12500},
    {"freq": 224_800_000, "name": "1.25m Repeater", "mod": "nfm", "group": "HAM 1.25m", "bandwidth": 12500},
]

# Default scan list — all bands
ALL_CHANNELS = (
    FM_BROADCAST + AIR_BAND + WX_SATELLITES + HAM_2M + MURS + BUSINESS_VHF
    + MARINE + RAILROAD + NOAA_WEATHER + HAM_125M + ISM_433 + HAM_70CM
    + PUBLIC_SAFETY + GMRS_CHANNELS + FRS_EXTRA_CHANNELS + ISM_900 + HAM_33CM
    + ADSB + HAM_23CM
)


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
    {"name": "FM", "groups": ["FM"]},
    {"name": "Air", "groups": ["Air"]},
    {"name": "WX Sat", "groups": ["WX Sat"]},
    {"name": "HAM 2m", "groups": ["HAM 2m"]},
    {"name": "MURS", "groups": ["MURS"]},
    {"name": "Business", "groups": ["Business"]},
    {"name": "Marine", "groups": ["Marine"]},
    {"name": "Railroad", "groups": ["Railroad"]},
    {"name": "NOAA", "groups": ["NOAA"]},
    {"name": "HAM 1.25m", "groups": ["HAM 1.25m"]},
    {"name": "ISM 433", "groups": ["ISM 433"]},
    {"name": "HAM 70cm", "groups": ["HAM 70cm"]},
    {"name": "Public Safety", "groups": ["Public Safety"]},
    {"name": "GMRS", "groups": ["GMRS"]},
    {"name": "FRS", "groups": ["FRS"]},
    {"name": "ISM 900", "groups": ["ISM 900"]},
    {"name": "HAM 33cm", "groups": ["HAM 33cm"]},
    {"name": "ADS-B", "groups": ["ADS-B"]},
    {"name": "HAM 23cm", "groups": ["HAM 23cm"]},
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
