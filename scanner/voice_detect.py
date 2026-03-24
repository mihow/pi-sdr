"""
Voice activity detection for SDR audio.
Two-layer approach:
  1. Spectral pre-filter (rejects digital modes and noise)
  2. WebRTC VAD (confirms human voice)

Works with 16-bit PCM mono audio at 16kHz from OpenWebRX+ or rtl_fm.
"""

from __future__ import annotations

import numpy as np
from collections import deque
from enum import Enum


class Detection(Enum):
    VOICE = "voice"
    DIGITAL = "digital"
    NOISE = "noise"
    PENDING = "pending"


class VoiceDetector:
    """
    Combined spectral + VAD voice detector.
    Processes 30ms frames of 16-bit PCM audio.
    """

    def __init__(
        self,
        sample_rate: int = 12000,
        frame_ms: int = 30,
        # Spectral thresholds — tuned for FM-demodulated audio from SDR
        # FM demod adds noise that raises flatness, so thresholds are higher
        flatness_digital: float = 0.65,
        flatness_noise: float = 0.85,
        zcr_noise: float = 0.45,
        # Decision window
        window_frames: int = 30,  # ~0.9s at 30ms
        voice_confirm_ratio: float = 0.3,
    ):
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2  # 16-bit
        self.flatness_digital = flatness_digital
        self.flatness_noise = flatness_noise
        self.zcr_noise = zcr_noise
        self.window: deque[Detection] = deque(maxlen=window_frames)
        self.voice_confirm_ratio = voice_confirm_ratio

        # Try to load webrtcvad, fall back to spectral-only
        self.vad = None
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(2)  # aggressiveness 2
        except ImportError:
            pass

    def _spectral_flatness(self, samples: np.ndarray) -> float:
        """Wiener entropy: geometric/arithmetic mean of power spectrum."""
        magnitude = np.abs(np.fft.rfft(samples))
        power = magnitude ** 2
        power = power[power > 0]
        if len(power) == 0:
            return 1.0
        geo = np.exp(np.mean(np.log(power)))
        arith = np.mean(power)
        return float(geo / arith) if arith > 0 else 1.0

    def _zcr(self, samples: np.ndarray) -> float:
        """Zero-crossing rate."""
        signs = np.sign(samples)
        return float(np.sum(np.abs(np.diff(signs)) > 0) / len(samples))

    def _has_pitch(self, samples: np.ndarray) -> bool:
        """Autocorrelation pitch detection (80-400 Hz)."""
        frame = samples - np.mean(samples)
        if np.max(np.abs(frame)) < 1e-6:
            return False
        corr = np.correlate(frame, frame, mode="full")
        corr = corr[len(corr) // 2:]
        corr = corr / (corr[0] + 1e-10)
        min_lag = self.sample_rate // 400
        max_lag = min(self.sample_rate // 80, len(corr) - 1)
        if min_lag >= max_lag:
            return False
        segment = corr[min_lag:max_lag]
        return bool(np.max(segment) > 0.2)

    def process_frame(self, frame_bytes: bytes) -> Detection:
        """Process one frame of 16-bit PCM audio. Returns frame detection."""
        samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        rms = float(np.sqrt(np.mean(samples ** 2)))
        sf = self._spectral_flatness(samples)
        zcr = self._zcr(samples)

        # Periodic debug
        if not hasattr(self, '_frame_count'):
            self._frame_count = 0
        self._frame_count += 1
        if self._frame_count % 20 == 1:
            import logging
            logging.getLogger(__name__).debug(
                "Frame: rms=%.4f sf=%.3f zcr=%.3f pitch=%s",
                rms, sf, zcr, self._has_pitch(samples) if rms > 0.003 else "skip"
            )

        # Combined detection: webrtcvad + spectral features
        # webrtcvad alone is too permissive on FM-demodulated audio (classifies
        # carrier noise as speech). Require BOTH webrtcvad AND spectral evidence.
        if rms < 0.003:
            det = Detection.NOISE
        else:
            has_pitch = self._has_pitch(samples) if rms > 0.01 else False
            vad_speech = False

            if self.vad is not None:
                try:
                    vad_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
                except Exception:
                    pass

            # High spectral flatness = noise or digital
            if sf > self.flatness_noise:
                det = Detection.NOISE
            elif sf > self.flatness_digital and not has_pitch:
                det = Detection.DIGITAL
            elif vad_speech and (has_pitch or sf < 0.35):
                # webrtcvad says speech AND spectral evidence supports it
                det = Detection.VOICE
            elif has_pitch and sf < 0.4:
                # Clear pitch + low flatness = voice even without webrtcvad
                det = Detection.VOICE
            else:
                det = Detection.NOISE

        self.window.append(det)
        return det

    def get_decision(self) -> tuple[Detection, float]:
        """Get overall detection from sliding window. Returns (detection, confidence)."""
        if len(self.window) < 10:
            return Detection.PENDING, 0.0

        counts = {d: 0 for d in Detection}
        for d in self.window:
            counts[d] += 1
        total = len(self.window)

        voice_ratio = counts[Detection.VOICE] / total
        digital_ratio = counts[Detection.DIGITAL] / total

        if voice_ratio >= self.voice_confirm_ratio:
            return Detection.VOICE, voice_ratio
        elif digital_ratio > 0.4:
            return Detection.DIGITAL, digital_ratio
        else:
            return Detection.NOISE, counts[Detection.NOISE] / total

    def reset(self):
        """Reset detection window (call when changing frequency)."""
        self.window.clear()
