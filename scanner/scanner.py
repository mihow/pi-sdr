"""
Core scanner engine. Uses direct SDR access via SoapySDR to scan frequencies,
detect signals via wideband FFT, and identify voice transmissions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .audio_stream import AudioBroadcaster
from .demod import demod_channel
from .fft_scan import compute_channel_power, find_peaks
from .frequencies import get_band_windows, get_default_scan_list
from .recorder import VoiceRecorder
from .sdr_backend import SdrBackend
from .voice_detect import Detection, VoiceDetector

log = logging.getLogger(__name__)


@dataclass
class ChannelState:
    freq: int
    name: str
    mod: str
    group: str
    bandwidth: int = 12500
    skip: bool = False
    last_signal: datetime | None = None
    last_voice: datetime | None = None
    signal_count: int = 0
    voice_count: int = 0
    last_smeter: float = -120.0


@dataclass
class ScannerState:
    scanning: bool = False
    paused_on_voice: bool = False
    current_freq: int = 0
    current_channel: str = ""
    current_smeter: float = -120.0
    current_detection: str = "pending"
    current_confidence: float = 0.0
    channels: dict[int, ChannelState] = field(default_factory=dict)
    scan_speed: float = 0.5  # seconds per channel when no signal
    voice_hold_time: float = 5.0  # seconds to stay on voice after it stops
    squelch_level: float = -45.0  # dB, signals above this are "active"
    # New fields for direct SDR scanning
    sdr_connected: bool = False
    current_band: str = ""
    scan_index: int = 0
    scan_total: int = 0
    channel_dwell_start: float = 0.0
    activity_log: deque = field(default_factory=lambda: deque(maxlen=100))
    scan_cycle_time: float = 0.0
    sdr_source: str = ""
    auto_discover: bool = False
    current_window_center: int = 0      # Hz, center of current FFT window
    current_window_bw: int = 2_400_000  # Hz, bandwidth of current window
    fft_data: list = field(default_factory=list)  # power spectrum for current window


class Scanner:
    """
    Frequency scanner that uses direct SDR access for wideband scanning.

    Tunes the SDR to band windows, performs FFT-based signal detection
    across all channels simultaneously, and identifies voice using
    spectral analysis + VAD on FM-demodulated audio.
    """

    def __init__(
        self,
        backend: SdrBackend,
        broadcaster: AudioBroadcaster | None = None,
        recorder: VoiceRecorder | None = None,
    ):
        self.backend = backend
        self.broadcaster = broadcaster
        self.recorder = recorder
        self.state = ScannerState()
        self.detector = VoiceDetector(sample_rate=16000)
        self._scan_thread: threading.Thread | None = None
        self._listen_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Load default channels
        for ch in get_default_scan_list():
            self.state.channels[ch["freq"]] = ChannelState(
                freq=ch["freq"],
                name=ch["name"],
                mod=ch["mod"],
                group=ch["group"],
                bandwidth=ch.get("bandwidth", 12500),
            )

    def add_channel(self, freq: int, name: str = "", group: str = "Discovered",
                    mod: str = "nfm", bandwidth: int = 12500) -> ChannelState | None:
        """Add a new channel. Returns None if freq already exists."""
        if freq in self.state.channels:
            return None  # Already exists
        if not name:
            name = f"{freq / 1e6:.4f} MHz"
        ch = ChannelState(freq=freq, name=name, mod=mod, group=group, bandwidth=bandwidth)
        self.state.channels[freq] = ch
        return ch

    def rename_channel(self, freq: int, name: str) -> bool:
        """Rename a channel."""
        if freq in self.state.channels:
            self.state.channels[freq].name = name
            return True
        return False

    def set_channel_params(self, freq: int, mod: str | None = None,
                           bandwidth: int | None = None) -> bool:
        """Update modulation type and/or bandwidth for a channel."""
        if freq not in self.state.channels:
            return False
        ch = self.state.channels[freq]
        if mod is not None and mod in ("nfm", "wfm", "am", "usb", "lsb"):
            ch.mod = mod
        if bandwidth is not None and 1000 <= bandwidth <= 250_000:
            ch.bandwidth = bandwidth
        return True

    def _get_scan_list(self) -> list[ChannelState]:
        """Get list of non-skipped channels."""
        return [ch for ch in self.state.channels.values() if not ch.skip]

    def start_scanning(self):
        """Start the scan loop in a background thread."""
        # Stop any existing listen/scan thread first
        self._stop_all_threads()
        self._stop_event.clear()
        self.state.scanning = True
        self.state.sdr_source = self.backend.get_source_name()
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        log.info("Scanning started")

    def stop_scanning(self):
        """Stop scanning."""
        self.state.scanning = False
        self._stop_event.set()
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=2)
        log.info("Scanning stopped")

    def _stop_all_threads(self):
        """Stop any running scan or listen threads."""
        self._stop_event.set()
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=2)
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2)
        self.state.scanning = False

    def _scan_loop(self):
        """Main scan loop: for each band window, tune, read IQ, FFT, check channels."""
        windows = get_band_windows(self.backend.get_max_bandwidth())

        # Count total channels across all windows for progress tracking
        scan_list = self._get_scan_list()
        scan_freqs = {ch.freq for ch in scan_list}
        self.state.scan_total = sum(
            len([c for c in w["channels"] if c["freq"] in scan_freqs])
            for w in windows
        )

        if self.state.scan_total == 0:
            log.warning("No channels to scan")
            return

        self.state.sdr_connected = self.backend.is_open()
        log.info(
            "Scan loop starting: %d windows, %d channels",
            len(windows), self.state.scan_total,
        )

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()

            # Refresh scan list each cycle (skip states may change)
            scan_list = self._get_scan_list()
            scan_freqs = {ch.freq for ch in scan_list}
            self.state.scan_index = 0

            for window in windows:
                if self._stop_event.is_set():
                    break

                # Get channels for this window that are not skipped
                window_channels = [
                    self.state.channels[c["freq"]]
                    for c in window["channels"]
                    if c["freq"] in scan_freqs and c["freq"] in self.state.channels
                ]
                if not window_channels:
                    continue

                # Tune to band center
                try:
                    self.backend.tune(window["center"])
                except Exception as e:
                    log.error("Tune failed for %s: %s", window["name"], e)
                    self.state.sdr_connected = False
                    continue

                self.state.current_band = window["name"]
                self.state.sdr_connected = True
                self.state.current_window_center = window["center"]
                self.state.current_window_bw = self.backend.get_sample_rate()
                time.sleep(0.05)  # settling time after retune

                # Read IQ and FFT scan all channels in this window
                try:
                    iq = self.backend.read_iq(262144)
                except Exception as e:
                    log.error("IQ read failed for %s: %s", window["name"], e)
                    self.state.sdr_connected = False
                    continue

                powers = compute_channel_power(
                    iq,
                    self.backend.get_sample_rate(),
                    window["center"],
                    [ch.freq for ch in window_channels],
                    [ch.bandwidth for ch in window_channels],
                )

                # Store FFT data for visualization (downsampled to ~512 points)
                try:
                    from .fft_scan import _compute_power_spectrum
                    fft_power_db = _compute_power_spectrum(iq, self.backend.get_sample_rate())
                    self.state.fft_data = fft_power_db
                except Exception:
                    pass

                # Update S-meter for all channels
                for ch in window_channels:
                    if ch.freq in powers:
                        ch.last_smeter = powers[ch.freq]
                        self.state.scan_index += 1

                # Auto-discover unknown signals
                if self.state.auto_discover:
                    known = list(self.state.channels.keys())  # ALL known freqs, not just window
                    peaks = find_peaks(iq, self.backend.get_sample_rate(),
                                       window["center"], self.state.squelch_level,
                                       known_freqs=known)
                    for peak in peaks:
                        added = self.add_channel(peak["freq"], group=window["name"])
                        if added:
                            log.info("Auto-discovered signal at %.4f MHz (%.1f dB)",
                                     peak["freq"]/1e6, peak["power_db"])

                # Check for active signals
                for ch in window_channels:
                    if self._stop_event.is_set():
                        break

                    if ch.freq not in powers:
                        continue

                    if powers[ch.freq] > self.state.squelch_level:
                        # Signal detected
                        ch.last_signal = datetime.now(timezone.utc)
                        ch.signal_count += 1
                        self.state.current_freq = ch.freq
                        self.state.current_channel = ch.name
                        self.state.current_smeter = powers[ch.freq]
                        self._log_activity("signal", ch, powers[ch.freq])

                        log.info(
                            "Signal on %s (%.3f MHz) S=%.1f dB",
                            ch.name, ch.freq / 1e6, powers[ch.freq],
                        )

                        # Demod for voice detection
                        audio = demod_channel(
                            iq, self.backend.get_sample_rate(),
                            window["center"], ch.freq, ch.bandwidth,
                            mod=ch.mod,
                        )

                        # Stream audio while analyzing (user can hear what's being checked)
                        if self.broadcaster:
                            self.broadcaster.push_audio(audio.tobytes())

                        voice_found = self._analyze_audio(ch, audio)
                        if voice_found:
                            self._hold_on_voice(ch, window)

            self.state.scan_cycle_time = time.monotonic() - cycle_start
            self.state.scan_index = 0

    def _analyze_audio(self, ch: ChannelState, audio) -> bool:
        """Process demodulated audio through voice detector.

        Args:
            ch: Channel being analyzed.
            audio: int16 PCM numpy array from demod_channel.

        Returns:
            True if voice detected.
        """
        self.detector.reset()
        self.state.current_detection = "pending"

        detection, confidence = self._process_audio_frames(audio)
        self.state.current_detection = detection.value
        self.state.current_confidence = confidence

        if detection == Detection.VOICE and confidence >= 0.3:
            ch.last_voice = datetime.now(timezone.utc)
            ch.voice_count += 1
            self._log_activity("voice", ch, ch.last_smeter, confidence=confidence)
            log.info("VOICE on %s (%.3f MHz) conf=%.2f", ch.name, ch.freq / 1e6, confidence)
            return True

        if detection == Detection.DIGITAL and confidence >= 0.5:
            log.info("Digital signal on %s (conf=%.2f), skipping", ch.name, confidence)

        return False

    def _process_audio_frames(self, audio) -> tuple[Detection, float]:
        """Split int16 PCM numpy array into 30ms frames and feed to VoiceDetector.

        Args:
            audio: int16 numpy array of PCM audio at 16kHz.

        Returns:
            (Detection, confidence) from the voice detector.
        """
        frame_samples = self.detector.frame_samples  # 480 at 16kHz, 30ms
        audio_bytes = audio.tobytes()

        offset = 0
        while offset + self.detector.frame_bytes <= len(audio_bytes):
            frame = audio_bytes[offset : offset + self.detector.frame_bytes]
            self.detector.process_frame(frame)
            offset += self.detector.frame_bytes

        return self.detector.get_decision()

    def _hold_on_voice(self, ch: ChannelState, window: dict, max_hold: float = 60.0):
        """Stay on frequency, continuously demod + stream audio."""
        # Play channel ID beep
        if self.broadcaster:
            self.broadcaster.push_tone(freq_hz=800, duration_ms=150)

        # Start recording
        if self.recorder:
            self.recorder.start_recording(ch.name, ch.freq)

        last_voice_time = time.monotonic()
        last_signal_time = time.monotonic()
        hold_start = time.monotonic()
        self.state.channel_dwell_start = hold_start
        self.state.paused_on_voice = True
        self.state.current_freq = ch.freq
        self.state.current_channel = ch.name
        self.state.current_detection = "voice"

        log.info("Holding on %s (%.3f MHz)", ch.name, ch.freq / 1e6)

        while not self._stop_event.is_set():
            # Read fresh IQ
            try:
                iq = self.backend.read_iq(65536)  # ~27ms at 2.4 Msps
            except Exception as e:
                log.error("IQ read failed during hold: %s", e)
                break

            # Demod
            audio = demod_channel(
                iq, self.backend.get_sample_rate(),
                window["center"], ch.freq, ch.bandwidth,
            )
            audio_bytes = audio.tobytes()

            # Stream to browser
            if self.broadcaster:
                self.broadcaster.push_audio(audio_bytes)

            # Record
            if self.recorder and self.recorder.is_recording:
                self.recorder.write_audio(audio_bytes)

            # Voice detection
            detection, confidence = self._process_audio_frames(audio)
            self.state.current_detection = detection.value
            self.state.current_confidence = confidence

            if detection == Detection.VOICE:
                last_voice_time = time.monotonic()

            # Check signal level via FFT on the small IQ block
            powers = compute_channel_power(
                iq, self.backend.get_sample_rate(),
                window["center"], [ch.freq], [ch.bandwidth],
            )
            if ch.freq in powers:
                ch.last_smeter = powers[ch.freq]
                self.state.current_smeter = powers[ch.freq]
                if powers[ch.freq] > self.state.squelch_level:
                    last_signal_time = time.monotonic()

            # Release if signal has been gone for 2 seconds
            if time.monotonic() - last_signal_time > 2.0:
                log.info("Signal lost on %s, resuming scan", ch.name)
                break

            # Release if voice stopped for hold_time
            if time.monotonic() - last_voice_time > self.state.voice_hold_time:
                log.info("Voice ended on %s, resuming scan", ch.name)
                break

            # Max hold time -- prevent locking on carriers/repeaters forever
            if time.monotonic() - hold_start > max_hold:
                log.info("Max hold time on %s, resuming scan", ch.name)
                break

        self.state.paused_on_voice = False
        self.state.channel_dwell_start = 0.0

        if self.recorder and self.recorder.is_recording:
            path = self.recorder.stop_recording()
            if path:
                duration = time.monotonic() - hold_start
                self._log_activity(
                    "recording", ch, duration=round(duration, 1), path=str(path),
                )

    def tune_to_channel(self, freq: int):
        """Stop scanning and tune to a specific channel for continuous listening."""
        self._stop_all_threads()

        ch = self.state.channels.get(freq)
        if not ch:
            log.warning("Channel %d not found", freq)
            return

        self.state.current_freq = ch.freq
        self.state.current_channel = ch.name
        self.state.current_detection = "listening"

        # Find the band window containing this channel
        windows = get_band_windows(self.backend.get_max_bandwidth())
        target_window = None
        for w in windows:
            for wch in w.get("channels", []):
                if wch["freq"] == freq:
                    target_window = w
                    break
            if target_window:
                break

        if not target_window:
            # Fallback: tune directly to channel freq
            target_window = {"center": freq, "name": "Manual"}

        self._stop_event.clear()
        self._listen_thread = threading.Thread(
            target=self._listen_loop, args=(ch, target_window), daemon=True)
        self._listen_thread.start()

    def _listen_loop(self, ch: ChannelState, window: dict):
        """Continuous demod + stream for a single channel."""
        log.info("Listening on %s (%.3f MHz)", ch.name, ch.freq / 1e6)

        try:
            self.backend.tune(window["center"])
        except Exception as e:
            log.error("Tune failed: %s", e)
            return

        self.state.current_band = window["name"]
        self.state.current_window_center = window["center"]
        self.state.current_window_bw = self.backend.get_sample_rate()
        self.state.sdr_connected = True

        while not self._stop_event.is_set():
            try:
                iq = self.backend.read_iq(65536)  # ~27ms chunks
            except Exception as e:
                log.error("IQ read failed: %s", e)
                break

            # FFT for visualization
            try:
                from .fft_scan import _compute_power_spectrum
                self.state.fft_data = _compute_power_spectrum(iq, self.backend.get_sample_rate())
            except Exception:
                pass

            # Demod and stream audio
            audio = demod_channel(iq, self.backend.get_sample_rate(),
                                  window["center"], ch.freq, ch.bandwidth,
                                  mod=ch.mod)
            audio_bytes = audio.tobytes()

            if self.broadcaster:
                self.broadcaster.push_audio(audio_bytes)

            # Voice detection
            detection, confidence = self._process_audio_frames(audio)
            self.state.current_detection = detection.value
            self.state.current_confidence = confidence
            self.state.current_smeter = float(np.max(np.abs(audio))) / 32768.0 * 100  # rough level

        log.info("Stopped listening on %s", ch.name)

    def stop_listening(self):
        """Stop listening mode."""
        self._stop_event.set()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2)
        self.state.current_detection = "pending"

    def _log_activity(self, event_type: str, ch: ChannelState, power: float = 0, **kwargs):
        """Append an event to the activity log."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "channel": ch.name,
            "freq": ch.freq,
            "power": round(power, 1),
            **kwargs,
        }
        self.state.activity_log.append(entry)
        log.debug("Activity: %s", entry)

    def toggle_skip(self, freq: int) -> bool:
        """Toggle skip status for a channel. Returns new skip state."""
        if freq in self.state.channels:
            ch = self.state.channels[freq]
            ch.skip = not ch.skip
            return ch.skip
        return False

    def set_squelch(self, level: float):
        """Set squelch level in dB."""
        self.state.squelch_level = level

    def get_state_dict(self) -> dict:
        """Get scanner state as a JSON-serializable dict."""
        channels = []
        for ch in self.state.channels.values():
            channels.append({
                "freq": ch.freq,
                "freq_mhz": f"{ch.freq / 1e6:.4f}",
                "name": ch.name,
                "mod": ch.mod,
                "bandwidth": ch.bandwidth,
                "group": ch.group,
                "skip": ch.skip,
                "last_signal": ch.last_signal.isoformat() if ch.last_signal else None,
                "last_voice": ch.last_voice.isoformat() if ch.last_voice else None,
                "signal_count": ch.signal_count,
                "voice_count": ch.voice_count,
                "smeter": ch.last_smeter,
                "active": ch.freq == self.state.current_freq,
            })

        # Compute dwell elapsed time
        dwell_elapsed = 0.0
        if self.state.channel_dwell_start > 0:
            dwell_elapsed = round(time.monotonic() - self.state.channel_dwell_start, 1)

        return {
            "scanning": self.state.scanning,
            "paused_on_voice": self.state.paused_on_voice,
            "current_freq": self.state.current_freq,
            "current_freq_mhz": f"{self.state.current_freq / 1e6:.4f}" if self.state.current_freq else "",
            "current_channel": self.state.current_channel,
            "current_smeter": self.state.current_smeter,
            "current_detection": self.state.current_detection,
            "current_confidence": self.state.current_confidence,
            "squelch_level": self.state.squelch_level,
            "scan_speed": self.state.scan_speed,
            "voice_hold_time": self.state.voice_hold_time,
            "channels": channels,
            # New fields
            "sdr_connected": self.state.sdr_connected,
            "current_band": self.state.current_band,
            "scan_index": self.state.scan_index,
            "scan_total": self.state.scan_total,
            "channel_dwell_elapsed": dwell_elapsed,
            "activity_log": list(self.state.activity_log),
            "scan_cycle_time": round(self.state.scan_cycle_time, 2),
            "sdr_source": self.state.sdr_source,
            "auto_discover": self.state.auto_discover,
            "current_window_center": self.state.current_window_center,
            "current_window_bw": self.state.current_window_bw,
            "current_window_lo_mhz": f"{(self.state.current_window_center - self.state.current_window_bw // 2) / 1e6:.3f}" if self.state.current_window_center else "",
            "current_window_hi_mhz": f"{(self.state.current_window_center + self.state.current_window_bw // 2) / 1e6:.3f}" if self.state.current_window_center else "",
            "fft_data": self.state.fft_data,
            "sdr_info": self.backend.get_device_info() if self.backend else {},
        }

    def shutdown(self):
        """Clean shutdown."""
        self.stop_scanning()
        if self.recorder and self.recorder.is_recording:
            self.recorder.stop_recording()
