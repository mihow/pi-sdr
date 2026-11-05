"""A deterministic simulated week written straight into a store, for digest tests.

Running the radio scene for a week takes many minutes, so summary tests use this generator.
It writes transmissions, clips, transcripts and band rows with known patterns:

- BUSY (GMRS), HAM (2 m), AIR (airband): steady daily patterns.
- QUIET (VHF land mobile): busy every hour, silent for the last 4 hours of the week.
- NEW (FRS 467): first heard on the last day (day index 6), five transmissions.
- HAM gets a 5x burst at 03:00 UTC on the last day (unusual).
- CARRIER is flagged always-on and SPUR is a spur; both have large raw activity, so they
  would top the busiest list if the summarizer did not exclude them.
- REPEATER (a 2 m repeater output, CTCSS 100.0 Hz) carries one short voice exchange and
  four key-ups an hour from 07:00 to 23:59 UTC; one key-up an hour has no tone.
- Transcripts carry call signs: K7ABC and W7XYZ on HAM (all week at noon, more on the last
  day), KD7ABC on REPEATER, WRAB123 on BUSY and N123AB on AIR.
- Every hour has stage counters (survey hops, peaks, tracks opened, dwell seconds) and an
  uplink push; the last day evicts two clips.
"""

from __future__ import annotations

from sdrscan import rollup
from sdrscan.bandplan import BandPlan
from sdrscan.health import Health
from sdrscan.store import Store
from sdrscan.tracker import Transmission

from .conftest import T0

H = 3600
DAY = 86400
BUSY, HAM, AIR = 462_562_500, 146_520_000, 122_800_000
QUIET, NEW = 154_430_000, 467_612_500
CARRIER, SPUR = 151_100_000, 453_500_000
REPEATER, TONE = 146_940_000, 100.0
END = T0 + 7 * DAY


def _tx(
    plan: BandPlan,
    hz: int,
    t: float,
    dur: float,
    cls: str = "voice",
    snr: float = 21.3,
    tone: float | None = None,
):
    ch = plan.channel_for(hz)
    return Transmission(
        hz=ch.hz,
        band=ch.band,
        mode=ch.mode,
        t_start=t,
        t_end=t + dur,
        dur_ms=int(round(dur * 1000)),
        peak_snr_db=snr,
        med_snr_db=snr - 4.2,
        floor_db=-51.0,
        bw_hz=11_000.0,
        source="survey",
        cls=cls,
        ctcss_hz=tone,
    )


def _add(store: Store, tx: Transmission) -> int:
    i = store.add_tx(tx)
    rollup.on_tx_closed(store, tx)
    return i


def week_events(plan: BandPlan) -> list[Transmission]:
    out: list[Transmission] = []
    for d in range(7):
        for h in range(24):
            base = T0 + d * DAY + h * H
            if 6 <= h <= 20:
                for k in range(3):
                    out.append(_tx(plan, BUSY, base + 300 + k * 1100, 30 + (d + k) % 4))
            bursts = 10 if (d == 6 and h == 3) else 2
            for k in range(bursts):
                out.append(_tx(plan, HAM, base + 120 + k * 330, 20, snr=17.5))
            for k in range(4):
                out.append(_tx(plan, AIR, base + 60 + k * 870, 10, snr=14.2))
            if not (d == 6 and h >= 20):
                for k in range(6):
                    out.append(_tx(plan, QUIET, base + 30 + k * 580, 40, cls="unknown"))
            if 7 <= h <= 23:
                out.append(_tx(plan, REPEATER, base + 1500, 12, snr=26.0, tone=TONE))
                for k in range(4):
                    tone = None if k == 3 else TONE
                    out.append(_tx(plan, REPEATER, base + 200 + k * 800, 1.5, "keyup", 24.0, tone))
            if d == 0 and h < 2:
                out.append(_tx(plan, CARRIER, base, 3000, cls="unknown", snr=30.0))
                out.append(_tx(plan, SPUR, base + 10, 3000, cls="unknown", snr=12.0))
        if d == 6:
            for k in range(5):
                out.append(_tx(plan, NEW, T0 + d * DAY + 9 * H + k * 1700, 15))
    return sorted(out, key=lambda t: (t.t_start, t.hz))


def fake_health(now: float = END) -> Health:
    return Health(
        ts=now,
        up_s=7 * DAY,
        temp_c=52.4,
        throttled=0,
        mode="normal",
        flags=[],
        dongle_ok=True,
        iq_age_s=0.2,
        disk_free_pct=61.3,
        disk_free_mb=17_800.0,
        quota_used_mb=212.5,
        quota_mb=2048.0,
        db_mb=48.2,
        clock_ok=True,
        rss_mb=142.0,
    )


TEXTS = {
    BUSY: ("WRAB123 unit {i} copy, moving to staging", "unit {i} copy, moving to staging"),
    NEW: ("unit {i} copy, moving to staging",),
    HAM: ("kilo seven alpha bravo charlie mobile, go ahead", "W7XYZ this is K7ABC, clear"),
    REPEATER: ("KD7ABC monitoring the repeater",),
    AIR: ("November one two three alpha bravo, runway two seven", "roger, wilco"),
}


def _transcribed(tx: Transmission) -> bool:
    if tx.cls != "voice" or tx.hz not in TEXTS:
        return False
    if tx.t_start > END - DAY:
        return True
    return tx.hz == HAM and int(tx.t_start - T0) % DAY // H == 12  # noon, all week


def build_week(store: Store, plan: BandPlan, vclock) -> None:
    txs = week_events(plan)
    clip_tx = {}
    for tx in txs:
        tx_id = _add(store, tx)
        if _transcribed(tx):
            clip_tx[tx_id] = tx
    store.mark_channel(CARRIER, band=plan.band_for(CARRIER).id, always_on=True, t=T0)
    store.mark_channel(SPUR, band=plan.band_for(SPUR).id, spur=True, t=T0)
    for i, (tx_id, tx) in enumerate(sorted(clip_tx.items())):
        cid = store.add_clip(
            hz=tx.hz,
            t_start=tx.t_start,
            dur_ms=tx.dur_ms,
            nbytes=12_000,
            codec="opus",
            path=f"clips/x/{tx.hz}-{int(tx.t_start)}.opus",
            tier=1,
            sha256="0" * 64,
        )
        conf = 0.4 if i % 4 == 3 else 0.88
        texts = TEXTS[tx.hz]
        text = texts[i % len(texts)].format(i=i)
        tid = store.add_transcript(cid, tx_id, "fake", text, conf, 0.3, tx.t_end)
        store.add_calls_from_transcript(
            text, conf, hz=tx.hz, band=tx.band, t=tx.t_end, tx_id=tx_id, transcript_id=tid
        )
        if i in (5, 9):
            store.evict_clip(cid, END - 1800)
    for h in range(7 * 24):
        t = T0 + h * H + 1800
        store.count("survey_hops", 22_000 + 37 * (h % 11), t=t)
        store.count("peaks", 4_100 + 13 * (h % 7), t=t)
        store.count("opened", 30 + h % 9, t=t)
        store.count("dwell_s", 1_310.5, t=t)
        store.add_uplink(t, "s3", 1_400, 6_000, 200)
    store.flush()
    for h in range(7 * 24):
        hour = int(T0 + h * H)
        for band, occ in (("gmrs_frs_462", 3.1), ("airband", 1.4), ("fm_bcast", 41.0)):
            store.db.execute(
                "INSERT INTO band_hour VALUES (?,?,?,?,?,?)",
                (band, hour, occ + (h % 5) * 0.1, 40 + h % 7, -55.0, 96.0 - (h % 3)),
            )
    store.db.commit()
    for d in range(7):
        rollup.rollup_day(store, int(T0 + d * DAY))
    for h in range(7 * 24):
        rollup.update_profiles(store, int(T0 + h * H))
    vclock.set(END)
