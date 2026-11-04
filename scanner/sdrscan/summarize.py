"""Rule-based digest: what the remote dashboard shows, in a few KB of JSON.

The digest is the only thing pushed off the rig by default, so it must be small, bounded
and deterministic. Every list has a fixed cap, numbers are integers or have one decimal
place, keys are sorted, and sparklines are digit strings ("0013970..."), so a site with
500 busy channels still gzips to at most 4 KB (spec section 8).

Sections:

- ``now``: channels heard in the last 5 minutes, and the tuner state when known.
- ``windows.hour|day|week|all``: the busiest channels by active seconds (always-on carriers
  and spurs excluded), the busiest bands by occupancy, and coverage.
- ``new``: at least 3 transmissions in the last 24 h and none in the 7 days before.
- ``unusual``: an hour in the last 24 h whose activity is more than 3 deviations above its
  baseline. The baseline is the same hour of the week over the last 4 weeks once 4 weeks of
  history exist, and the same hour of the day over the last 7 days before that.
- ``quiet``: an all-time top-20 channel silent for at least 3 h, during at least 3 hours
  when its baseline says it is usually active.
- ``always_on``: carriers and spurs, kept apart so they never fill the busiest lists.
- ``all_time`` and ``how``: totals and a typical hour-of-week strip (168 digits).
- ``recent`` and ``transcripts`` (opt-in, confidence at least 0.6, 160 characters).
- ``health``: rig health, coverage and the uplink ledger.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics

from .bandplan import BandPlan
from .health import Health
from .rollup import DAY, H, day_of, hour_of, query_window
from .store import Store

SCHEMA = 1
MAX_GZ_BYTES = 4096
TOP_N = 10
LIST_N = 5
ALWAYS_N = 10
BANDS_N = 6
RECENT_N = 8
TRANSCRIPTS_N = 10
TRANSCRIPTS_PER_CHANNEL_DAY = 3
TEXT_MAX = 160
MIN_CONFIDENCE = 0.6
NOW_S = 300
Z_UNUSUAL = 3.0
UNUSUAL_FLOOR_S = 30.0
QUIET_SILENT_H = 3
QUIET_USUAL_S = 60.0
QUIET_TOP = 20


def r1(x: float | None) -> float | None:
    return None if x is None else round(float(x), 1)


def spark(cells: list[float]) -> str:
    """Cells scaled to digits 0..9 relative to the largest cell; 0 only when idle."""
    top = max(cells) if cells else 0
    if top <= 0:
        return "0" * len(cells)
    return "".join("0" if c <= 0 else str(max(1, min(9, round(9 * c / top)))) for c in cells)


def encode_digest(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def gzip_digest(d: dict) -> bytes:
    return gzip.compress(encode_digest(d), 9, mtime=0)


def build_digest(
    store: Store,
    health: Health | dict | None,
    now: float,
    *,
    plan: BandPlan | None = None,
    site_id: str = "site",
    include_transcripts: bool = False,
    now_state: dict | None = None,
    uplink: dict | None = None,
    prose: str | None = None,
) -> dict:
    store.flush()
    flags = {
        r["hz"]: ("spur" if r["spur"] else "carrier")
        for r in store.db.execute("SELECT hz, always_on, spur FROM channels")
        if r["always_on"] or r["spur"]
    }
    windows = {w: _window(store, w, now, flags) for w in ("hour", "day", "week", "all")}
    used_bands = set()
    hz_seen: set[int] = set()
    for w in windows.values():
        hz_seen.update(c["hz"] for c in w["top"])
        used_bands.update(b["band"] for b in w["bands"])
    baseline = _Baseline(store, now)
    d = {
        "v": SCHEMA,
        "site": site_id,
        "generated_at": int(now),
        "now": _now(store, now, flags, now_state),
        "windows": windows,
        "new": _new(store, now, flags),
        "unusual": _unusual(store, now, flags, baseline),
        "quiet": _quiet(store, now, flags, baseline),
        "always_on": _always_on(store, flags),
        "all_time": _all_time(store, now, flags),
        "how": _how(store),
        "recent": _recent(store, now, flags, include_transcripts),
        "health": _health(health, windows, uplink),
        "prose": prose,
    }
    if include_transcripts:
        d["transcripts"] = _transcripts(store, now)
    for section in ("now", "new", "unusual", "quiet", "recent"):
        items = d[section]["active"] if section == "now" else d[section]
        hz_seen.update(c["hz"] for c in items)
    names: dict[str, str] = {}
    if plan is not None:
        for hz in hz_seen:
            used_bands.add(plan.band_for(hz).id)
        for band in sorted(used_bands):
            names[band] = plan.band(band).name or band
        labels = {str(hz): plan.labels[hz] for hz in sorted(hz_seen) if hz in plan.labels}
        if labels:
            d["labels"] = labels
    d["band_names"] = names
    _band_ids(store, d)
    return d


# -- sections ----------------------------------------------------------------------------
def _band_ids(store: Store, d: dict) -> None:
    """Attach the band id to every channel entry, from the channels table."""
    band = {r["hz"]: r["band"] for r in store.db.execute("SELECT hz, band FROM channels")}

    def tag(items):
        for c in items:
            c.setdefault("band", band.get(c["hz"]) or "unplanned")

    for w in d["windows"].values():
        tag(w["top"])
    for key in ("new", "unusual", "quiet", "recent"):
        tag(d[key])
    tag(d["now"]["active"])
    tag(d["always_on"]["list"])


def _window(store: Store, window: str, now: float, flags: dict[int, str]) -> dict:
    ws = query_window(store, window, now)
    ranked = sorted(
        ((hz, c) for hz, c in ws.channels.items() if hz not in flags and c.active_s > 0),
        key=lambda kv: (-kv[1].active_s, kv[0]),
    )
    top = []
    for hz, c in ranked[:TOP_N]:
        item = {
            "hz": hz,
            "active_s": int(round(c.active_s)),
            "tx": int(c.tx_count),
            "voice": int(c.voice_tx),
        }
        if c.max_snr_db is not None:
            item["snr"] = r1(c.max_snr_db)
        if hz in ws.series:
            item["spark"] = spark(ws.series[hz])
        top.append(item)
    t0 = ws.t0 if window != "all" else now - 400 * DAY
    bands, coverage = _bands(store, t0, now)
    return {
        "top": top,
        "channels": len(ranked),
        "active_s": int(round(sum(c.active_s for _, c in ranked))),
        "tx": int(sum(c.tx_count for _, c in ranked)),
        "bands": bands,
        "coverage_pct": coverage,
        "cell_s": ws.cell_s,
    }


def _bands(store: Store, t0: float, now: float) -> tuple[list[dict], float | None]:
    rows = store.db.execute(
        "SELECT band, AVG(occupancy_pct) occ, SUM(peak_count) peaks FROM band_hour"
        " WHERE hour_ts >= ? AND hour_ts <= ? GROUP BY band",
        (hour_of(t0), hour_of(now)),
    ).fetchall()
    ranked = sorted(rows, key=lambda r: (-(r["occ"] or 0), r["band"]))[:BANDS_N]
    cov = store.db.execute(
        "SELECT AVG(c) FROM (SELECT hour_ts, MIN(coverage_pct) c FROM band_hour"
        " WHERE hour_ts >= ? AND hour_ts <= ? AND coverage_pct IS NOT NULL GROUP BY hour_ts)",
        (hour_of(t0), hour_of(now)),
    ).fetchone()[0]
    return (
        [
            {"band": r["band"], "occ_pct": r1(r["occ"]), "peaks": int(r["peaks"] or 0)}
            for r in ranked
        ],
        r1(cov),
    )


def _now(store: Store, now: float, flags: dict[int, str], state: dict | None) -> dict:
    active = []
    if state is not None and state.get("active") is not None:
        active = [a for a in state["active"] if a["hz"] not in flags]
    else:
        for r in store.db.execute(
            "SELECT hz, MAX(t_end) t, MAX(peak_snr_db) snr, class FROM transmissions"
            " WHERE t_end >= ? GROUP BY hz",
            (int(now - NOW_S),),
        ):
            if r["hz"] in flags:
                continue
            active.append(
                {
                    "hz": r["hz"],
                    "age_s": max(0, int(now - r["t"])),
                    "snr": r1(r["snr"]),
                    "cls": r["class"] or "unknown",
                }
            )
    active.sort(key=lambda a: (a.get("age_s", 0), -(a.get("snr") or 0), a["hz"]))
    out = {"active": active[:TOP_N], "count": len(active)}
    if state is not None and state.get("tuner"):
        out["tuner"] = state["tuner"]
    return out


def _new(store: Store, now: float, flags: dict[int, str]) -> list[dict]:
    t_win = now - DAY
    first = store.db.execute("SELECT MIN(t_start) FROM transmissions").fetchone()[0]
    if first is None or first > t_win - DAY:
        return []  # less than a day of history before the window: everything would be new
    rows = store.db.execute(
        "SELECT hz, COUNT(*) n, MIN(t_start) f FROM transmissions WHERE t_start >= ?"
        " GROUP BY hz HAVING n >= 3",
        (int(t_win),),
    ).fetchall()
    out = []
    for r in rows:
        if r["hz"] in flags:
            continue
        prior = store.db.execute(
            "SELECT 1 FROM transmissions WHERE hz = ? AND t_start >= ? AND t_start < ? LIMIT 1",
            (r["hz"], int(t_win - 7 * DAY), int(t_win)),
        ).fetchone()
        if prior is None:
            out.append({"hz": r["hz"], "tx": r["n"], "first_ts": r["f"]})
    out.sort(key=lambda x: (-x["tx"], x["hz"]))
    return out[:LIST_N]


class _Baseline:
    """Usual active seconds of a channel in a given hour, from earlier rollup_hour rows."""

    def __init__(self, store: Store, now: float) -> None:
        first = store.db.execute("SELECT MIN(hour_ts) FROM rollup_hour").fetchone()[0]
        self.first = first if first is not None else hour_of(now)
        self.weekly = (now - self.first) >= 5 * 7 * DAY
        lookback = (4 * 7 + 1) * DAY if self.weekly else 8 * DAY
        self.rows: dict[tuple[int, int], float] = {
            (r["hz"], r["hour_ts"]): r["active_s"]
            for r in store.db.execute(
                "SELECT hz, hour_ts, active_s FROM rollup_hour WHERE hour_ts >= ?",
                (hour_of(now - lookback),),
            )
        }

    def value(self, hz: int, hour_ts: int) -> float:
        return self.rows.get((hz, hour_ts), 0.0)

    def stats(self, hz: int, hour_ts: int) -> tuple[float, float] | None:
        step, k = (7 * DAY, 4) if self.weekly else (DAY, 7)
        xs = [
            self.value(hz, hour_ts - i * step)
            for i in range(1, k + 1)
            if hour_ts - i * step >= self.first
        ]
        if len(xs) < 2:
            return None
        return statistics.fmean(xs), statistics.pstdev(xs)


def _unusual(store: Store, now: float, flags: dict[int, str], base: _Baseline) -> list[dict]:
    h_now = hour_of(now)
    out = []
    for (hz, h), x in base.rows.items():
        if h < h_now - 24 * H or h >= h_now or hz in flags:
            continue
        st = base.stats(hz, h)
        if st is None:
            continue
        mean, sd = st
        z = (x - mean) / max(sd, 0.5 * mean, UNUSUAL_FLOOR_S)
        if z > Z_UNUSUAL:
            out.append(
                {
                    "hz": hz,
                    "hour": h,
                    "active_s": int(round(x)),
                    "usual_s": int(round(mean)),
                    "z": r1(z),
                }
            )
    out.sort(key=lambda u: (-u["z"], u["hz"], u["hour"]))
    return out[:LIST_N]


def _quiet(store: Store, now: float, flags: dict[int, str], base: _Baseline) -> list[dict]:
    rows = store.db.execute(
        "SELECT t.hz, t.active_s, c.last_seen FROM (SELECT hz, active_s FROM channel_total"
        " UNION ALL SELECT hz, SUM(active_s) FROM rollup_hour WHERE hour_ts >= ? GROUP BY hz) t"
        " JOIN channels c ON c.hz = t.hz",
        (int(store.meta("rolled_through") or day_of(now)),),
    ).fetchall()
    total: dict[int, float] = {}
    last: dict[int, int] = {}
    for r in rows:
        if r["hz"] in flags:
            continue
        total[r["hz"]] = total.get(r["hz"], 0.0) + (r["active_s"] or 0.0)
        last[r["hz"]] = r["last_seen"]
    top = sorted(total, key=lambda hz: (-total[hz], hz))[:QUIET_TOP]
    h_now = hour_of(now)
    out = []
    for hz in top:
        silent_h = int((now - (last[hz] or now)) // H)
        if silent_h < QUIET_SILENT_H:
            continue
        usual = []
        for i in range(1, min(silent_h, 24) + 1):
            st = base.stats(hz, h_now - i * H)
            if st is not None and st[0] >= QUIET_USUAL_S:
                usual.append(st[0])
        if len(usual) >= QUIET_SILENT_H:
            out.append({"hz": hz, "silent_h": silent_h, "usual_s": int(round(sum(usual)))})
    out.sort(key=lambda q: (-q["usual_s"], q["hz"]))
    return out[:LIST_N]


def _always_on(store: Store, flags: dict[int, str]) -> dict:
    items = [{"hz": hz, "kind": kind} for hz, kind in sorted(flags.items())]
    return {"count": len(items), "list": items[:ALWAYS_N]}


def _all_time(store: Store, now: float, flags: dict[int, str]) -> dict:
    ws = query_window(store, "all", now)
    chans = {hz: c for hz, c in ws.channels.items() if hz not in flags}
    since = store.db.execute("SELECT MIN(first_seen) FROM channels").fetchone()[0]
    return {
        "since": since,
        "channels": len(chans),
        "tx": int(sum(c.tx_count for c in chans.values())),
        "active_h": r1(sum(c.active_s for c in chans.values()) / H),
        "voice_tx": int(sum(c.voice_tx for c in chans.values())),
    }


def _how(store: Store) -> str:
    rows = store.db.execute(
        "SELECT p.how, SUM(p.ema_active_s) s FROM profile_how p JOIN channels c ON c.hz = p.hz"
        " WHERE c.always_on = 0 AND c.spur = 0 GROUP BY p.how"
    ).fetchall()
    cells = [0.0] * 168
    for r in rows:
        cells[r["how"]] = r["s"] or 0.0
    return spark(cells)


def _recent(store: Store, now: float, flags: dict[int, str], with_text: bool) -> list[dict]:
    rows = store.db.execute(
        "SELECT t.hz, t.t_start, t.dur_ms, t.class, t.peak_snr_db, c.id clip, c.path,"
        " r.text, r.confidence FROM transmissions t LEFT JOIN clips c ON c.id = t.clip_id"
        " LEFT JOIN transcripts r ON r.id = t.transcript_id WHERE t.t_start >= ?"
        " ORDER BY t.t_start DESC, t.hz LIMIT 200",
        (int(now - DAY),),
    ).fetchall()
    out = []
    for r in rows:
        if r["hz"] in flags:
            continue
        item = {
            "hz": r["hz"],
            "t": r["t_start"],
            "dur_s": r1(r["dur_ms"] / 1000),
            "cls": r["class"] or "unknown",
            "snr": r1(r["peak_snr_db"]),
        }
        if r["clip"] is not None and r["path"]:
            item["clip"] = r["clip"]
        if with_text and r["text"] and (r["confidence"] or 0) >= MIN_CONFIDENCE:
            item["text"] = _clip_text(r["text"])
            item["conf"] = r1(r["confidence"])
        out.append(item)
        if len(out) >= RECENT_N:
            break
    return out


def _transcripts(store: Store, now: float) -> list[dict]:
    rows = store.db.execute(
        "SELECT r.text, r.confidence, r.created, c.hz FROM transcripts r JOIN clips c"
        " ON c.id = r.clip_id WHERE r.created >= ? AND r.confidence >= ?"
        " ORDER BY r.created DESC, c.hz",
        (int(now - DAY), MIN_CONFIDENCE),
    ).fetchall()
    per: dict[int, int] = {}
    out = []
    for r in rows:
        if per.get(r["hz"], 0) >= TRANSCRIPTS_PER_CHANNEL_DAY:
            continue
        per[r["hz"]] = per.get(r["hz"], 0) + 1
        out.append(
            {
                "hz": r["hz"],
                "t": r["created"],
                "text": _clip_text(r["text"]),
                "conf": r1(r["confidence"]),
            }
        )
        if len(out) >= TRANSCRIPTS_N:
            break
    return out


def _clip_text(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1].rstrip() + "…"


def _health(health: Health | dict | None, windows: dict, uplink: dict | None) -> dict:
    if health is None:
        h: dict = {}
    elif isinstance(health, Health):
        h = health.to_digest()
    else:
        h = dict(health)
    h["coverage_pct"] = windows["hour"]["coverage_pct"]
    if uplink is not None:
        h["uplink"] = {
            "month_kb": int(math.ceil(uplink.get("month_bytes", 0) / 1000)),
            "cap_kb": int(uplink.get("cap_bytes", 0) // 1000),
            "state": uplink.get("state", "ok"),
            # The page derives "late" and "offline" from the push interval.
            "interval_s": int(uplink.get("interval_s", 3600)),
        }
    return h
