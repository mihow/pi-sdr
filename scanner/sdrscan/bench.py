"""Time the hot paths on this machine, so the Pi's real costs can be measured on site.

No Pi figure exists yet (spec section 16). ``sdrscan bench --json`` reports the survey hop
analysis time, the channelizer and demodulator cost per channel per 100 ms block, and the
voice gate cost per 30 ms frame, on synthetic IQ.
"""

from __future__ import annotations

import numpy as np

from . import AUDIO_RATE, IQ_RATE
from .bandplan import BandPlan
from .clock import RealClock, VirtualClock
from .config import DEFAULT_BANDPLAN, DetectConfig
from .detect import analyze
from .dsp.channelizer import Channelizer
from .iq.scene import Scene
from .iq.synthetic import SyntheticSource
from .voice.gate import VoiceGate

CENTER = 462_400_000
CHANNELS = (462_562_500, 462_637_500, 462_712_500, 462_187_500)


def _timed(fn, seconds: float, wall: RealClock) -> float:
    """Mean milliseconds per call, repeating for about ``seconds``."""
    fn()  # warm caches (FIR taps, FFT plans)
    n, t0 = 0, wall.monotonic()
    while True:
        fn()
        n += 1
        if wall.monotonic() - t0 >= seconds:
            return round((wall.monotonic() - t0) / n * 1000, 3)


def run_bench(seconds: float = 1.0) -> dict:
    wall = RealClock()
    clock = VirtualClock(0.0)
    emitters = "".join(
        f"  - {{kind: nfm_voice, hz: {hz}, level_db: -35, bursts: [[0, 1000000]]}}\n"
        for hz in CHANNELS
    )
    scene = Scene.from_yaml(f"noise_floor: {{default_db: -45}}\nemitters:\n{emitters}", clock)
    src = SyntheticSource(scene, clock)
    src.open()
    src.tune(CENTER, 30.0)
    block = src.read(IQ_RATE // 10)
    plan = BandPlan.load(DEFAULT_BANDPLAN)
    cfg = DetectConfig()
    hop_ms = _timed(lambda: analyze(block, CENTER, IQ_RATE, plan, cfg), seconds, wall)
    chans = [plan.channel_for(hz) for hz in CHANNELS]
    chz = Channelizer(IQ_RATE, CENTER, chans)
    demod_ms = _timed(lambda: chz.push(block), seconds, wall) / len(chans)
    audio = chz.push(block)[CHANNELS[0]]
    frames = np.tile(audio, 3)[: AUDIO_RATE * 3 // 10]  # ten 30 ms frames
    gate = VoiceGate()
    vad_ms = _timed(lambda: gate.push(frames), seconds, wall) / 10
    render_ms = _timed(lambda: src.read(IQ_RATE // 10), seconds, wall)
    return {
        "hop_analyze_ms_per_100ms": hop_ms,
        "demod_ms_per_channel_per_100ms": round(demod_ms, 3),
        "vad_ms_per_frame": round(vad_ms, 3),
        "synthetic_render_ms_per_100ms": render_ms,
        "realtime_budget_ms_per_100ms": 100.0,
    }
