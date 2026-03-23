"""
Core scanner engine. Controls OpenWebRX+ via WebSocket to scan frequencies,
detect signals, and identify voice transmissions.
"""

from __future__ import annotations

import json
import logging
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websocket

from .frequencies import get_default_scan_list
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
    squelch_level: float = -60.0  # dB, signals above this are "active"


class Scanner:
    """
    Frequency scanner that uses OpenWebRX+ WebSocket for SDR control.

    Connects to OpenWebRX+, cycles through frequencies, detects signals
    via S-meter, and identifies voice using spectral analysis + VAD.
    """

    # Map frequency groups to OpenWebRX+ profile names and center frequencies
    PROFILES = {
        "GMRS": {"name": "GMRS/FRS", "center": 462_562_500, "bw": 2_400_000},
        "FRS": {"name": "GMRS/FRS", "center": 462_562_500, "bw": 2_400_000},
        "HAM 2m": {"name": "2m Ham", "center": 146_000_000, "bw": 2_400_000},
        "HAM 70cm": {"name": "70cm Ham", "center": 446_000_000, "bw": 2_400_000},
        "MURS": {"name": "Marine VHF", "center": 157_000_000, "bw": 2_400_000},
        "Marine": {"name": "Marine VHF", "center": 157_000_000, "bw": 2_400_000},
        "NOAA": {"name": "NOAA Weather", "center": 162_475_000, "bw": 2_400_000},
    }

    def __init__(self, owrx_host: str = "localhost", owrx_port: int = 8073, use_ssl: bool = False):
        scheme = "wss" if use_ssl else "ws"
        self.owrx_url = f"{scheme}://{owrx_host}:{owrx_port}/ws/"
        self.use_ssl = use_ssl
        self.state = ScannerState()
        self.detector = VoiceDetector(sample_rate=16000)
        self.ws: websocket.WebSocket | None = None
        self._scan_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._audio_buffer = bytearray()
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._smeter_updated = threading.Event()
        self._current_profile: str | None = None
        self._current_center: int = 0
        self._sdr_id: str = ""
        self._profiles_map: dict = {}  # profile_name -> profile_id

        # Load default channels
        for ch in get_default_scan_list():
            self.state.channels[ch["freq"]] = ChannelState(
                freq=ch["freq"],
                name=ch["name"],
                mod=ch["mod"],
                group=ch["group"],
                bandwidth=ch.get("bandwidth", 12500),
            )

    def _reconnect(self):
        """Reconnect to OpenWebRX+ WebSocket."""
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        time.sleep(1)
        try:
            self.connect()
        except Exception as e:
            log.error("Reconnect failed: %s", e)

    def _get_scan_list(self) -> list[ChannelState]:
        """Get ordered list of non-skipped channels, grouped by profile to minimize switches."""
        channels = [ch for ch in self.state.channels.values() if not ch.skip]
        # Sort by profile name so all channels in one band are scanned together
        profile_order = list(self.PROFILES.keys())
        channels.sort(key=lambda c: (
            profile_order.index(c.group) if c.group in profile_order else 99,
            c.freq,
        ))
        return channels

    def connect(self):
        """Connect to OpenWebRX+ WebSocket."""
        log.info("Connecting to %s", self.owrx_url)
        sslopt = {}
        if self.use_ssl:
            sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        self.ws = websocket.WebSocket(sslopt=sslopt)
        self.ws.connect(self.owrx_url)

        # Handshake
        self.ws.send("SERVER DE CLIENT client=radio-scanner type=receiver")
        log.info("Connected to OpenWebRX+")
        self._connected.set()

        # Start receiving thread
        self._recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._recv_thread.start()

    def _receive_loop(self):
        """Background thread: receive WebSocket messages."""
        text_count = 0
        binary_count = 0
        while not self._stop_event.is_set():
            try:
                opcode, data = self.ws.recv_data()
                if opcode == websocket.ABNF.OPCODE_TEXT:
                    text_count += 1
                    self._handle_text(data.decode("utf-8"))
                elif opcode == websocket.ABNF.OPCODE_BINARY:
                    binary_count += 1
                    self._handle_binary(data)
                    if binary_count % 100 == 1:
                        log.debug("Recv stats: %d text, %d binary", text_count, binary_count)
                else:
                    log.debug("Unknown opcode: %d len=%d", opcode, len(data))
            except websocket.WebSocketConnectionClosedException:
                log.warning("WebSocket connection closed (text=%d bin=%d)", text_count, binary_count)
                break
            except Exception as e:
                if not self._stop_event.is_set():
                    log.error("Receive error: %s (text=%d bin=%d)", e, text_count, binary_count)
                break

    def _handle_text(self, message: str):
        """Handle JSON text messages from OpenWebRX+."""
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            # Might be the handshake response
            if "CLIENT DE SERVER" in message:
                log.info("Handshake: %s", message)
            return

        msg_type = msg.get("type")
        if msg_type == "smeter":
            raw = msg["value"]
            # OpenWebRX+ sends raw linear power — convert to dB
            import math
            if raw > 0:
                self.state.current_smeter = 10 * math.log10(raw)
            else:
                self.state.current_smeter = -120.0
            self._smeter_updated.set()
        elif msg_type == "config":
            val = msg.get("value", {})
            log.debug("Config: %s", json.dumps(val)[:200])
        elif msg_type == "profiles":
            # Capture available profiles: list of {"name", "id", ...}
            profiles = msg.get("value", [])
            for p in profiles:
                pid = p.get("id", "")
                pname = p.get("name", "")
                self._profiles_map[pname] = pid
                # Extract SDR ID from profile ID (format: "sdr_id|profile_id")
                if "|" in pid and not self._sdr_id:
                    self._sdr_id = pid.split("|")[0]
            log.info("Profiles: %s", list(self._profiles_map.keys()))
        elif msg_type == "sdr_error":
            log.error("SDR error: %s", msg.get("value"))

    def _handle_binary(self, data: bytes):
        """Handle binary messages (audio, FFT) from OpenWebRX+."""
        if len(data) < 1:
            return
        msg_type = data[0]
        if msg_type == 0x02:
            # Demodulated audio: 16-bit signed LE PCM at output_rate
            audio = data[1:]
            with self._lock:
                self._audio_buffer.extend(audio)
                if len(self._audio_buffer) % 5000 < len(audio):
                    log.debug("Audio buffer: %d bytes (+%d)", len(self._audio_buffer), len(audio))
        elif msg_type == 0x01:
            pass  # FFT data, ignore
        else:
            if not hasattr(self, '_bin_type_seen'):
                self._bin_type_seen = set()
            if msg_type not in self._bin_type_seen:
                self._bin_type_seen.add(msg_type)
                log.info("Binary type seen: 0x%02x len=%d (first occurrence)", msg_type, len(data))

    def _find_profile_id(self, target_name: str) -> str | None:
        """Find profile ID by partial name match."""
        # Exact match first
        if target_name in self._profiles_map:
            return self._profiles_map[target_name]
        # Partial match (profiles are prefixed with SDR name)
        for name, pid in self._profiles_map.items():
            if target_name in name or name.endswith(target_name):
                return pid
        return None

    def _select_profile(self, group: str):
        """Switch OpenWebRX+ to the SDR profile covering this frequency group."""
        profile_info = self.PROFILES.get(group)
        if not profile_info:
            return
        profile_name = profile_info["name"]
        if profile_name == self._current_profile:
            return

        # Find profile ID
        profile_id = self._find_profile_id(profile_name)
        if profile_id:
            log.info("Switching to profile: %s (%s)", profile_name, profile_id)
            try:
                self.ws.send(json.dumps({"type": "selectprofile", "params": {"profile": profile_id}}))
            except Exception as e:
                log.warning("Profile switch failed: %s", e)
                return
            self._current_profile = profile_name
            self._current_center = profile_info["center"]
            time.sleep(0.5)  # let SDR retune
            self._start_dsp()  # restart DSP after profile switch
            time.sleep(0.3)
        else:
            if self._profiles_map:
                log.warning("Profile not found: %s (available: %s)", profile_name, list(self._profiles_map.keys()))
            self._current_center = profile_info["center"]
            self._current_profile = profile_name

    def _tune(self, freq: int, mod: str = "nfm", bandwidth: int = 12500, group: str = ""):
        """Tune OpenWebRX+ to a frequency."""
        # Switch profile if needed
        self._select_profile(group)

        half_bw = bandwidth // 2
        offset = freq - self._current_center if self._current_center else freq
        params = {
            "type": "dspcontrol",
            "action": "start",
            "params": {
                "mod": mod,
                "offset_freq": offset,
                "low_cut": -half_bw,
                "high_cut": half_bw,
                "squelch_level": -150,
                "output_rate": 16000,
            },
        }
        try:
            self.ws.send(json.dumps(params))
        except Exception as e:
            log.warning("Send failed: %s, reconnecting...", e)
            self._reconnect()
        self.state.current_freq = freq

    def _process_audio(self) -> tuple[Detection, float]:
        """Process buffered audio through voice detector. Returns (detection, confidence)."""
        with self._lock:
            buf = bytes(self._audio_buffer)
            self._audio_buffer.clear()

        if len(buf) < self.detector.frame_bytes:
            return self.detector.get_decision()

        # Process all complete frames
        offset = 0
        while offset + self.detector.frame_bytes <= len(buf):
            frame = buf[offset:offset + self.detector.frame_bytes]
            self.detector.process_frame(frame)
            offset += self.detector.frame_bytes

        return self.detector.get_decision()

    def start_scanning(self):
        """Start the scan loop in a background thread."""
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._stop_event.clear()
        self.state.scanning = True
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        log.info("Scanning started")

    def stop_scanning(self):
        """Stop scanning."""
        self.state.scanning = False
        self._stop_event.set()
        log.info("Scanning stopped")

    def _start_dsp(self):
        """Send initial DSP start command to OpenWebRX+."""
        params = {
            "type": "dspcontrol",
            "action": "start",
            "params": {
                "mod": "nfm",
                "offset_freq": 0,
                "low_cut": -12500,
                "high_cut": 12500,
                "squelch_level": -150,
                "output_rate": 16000,
            },
        }
        try:
            self.ws.send(json.dumps(params))
            log.info("DSP started")
        except Exception as e:
            log.error("Failed to start DSP: %s", e)

    def _scan_loop(self):
        """Main scan loop: cycle through frequencies, detect voice."""
        # Wait for profiles to arrive from OpenWebRX+
        for _ in range(20):
            if self._profiles_map:
                break
            time.sleep(0.2)
        if not self._profiles_map:
            log.warning("No profiles received from OpenWebRX+ after 4s")

        scan_list = self._get_scan_list()
        if not scan_list:
            log.warning("No channels to scan")
            return

        idx = 0
        while not self._stop_event.is_set():
            scan_list = self._get_scan_list()
            if not scan_list:
                time.sleep(1)
                continue

            idx = idx % len(scan_list)
            ch = scan_list[idx]

            # Tune to channel
            self._tune(ch.freq, ch.mod, ch.bandwidth, ch.group)
            self.state.current_channel = ch.name
            self.state.current_detection = "pending"
            self.detector.reset()

            # Clear audio buffer
            with self._lock:
                self._audio_buffer.clear()

            # Wait for S-meter reading
            self._smeter_updated.clear()
            self._smeter_updated.wait(timeout=0.3)

            smeter = self.state.current_smeter
            ch.last_smeter = smeter

            if smeter > self.state.squelch_level:
                # Signal detected! Dwell and analyze
                ch.last_signal = datetime.now(timezone.utc)
                ch.signal_count += 1
                log.info("Signal on %s (%.3f MHz) S=%.1f dB",
                         ch.name, ch.freq / 1e6, smeter)

                voice_found = self._dwell_and_detect(ch)
                if voice_found:
                    ch.last_voice = datetime.now(timezone.utc)
                    ch.voice_count += 1
                    self.state.paused_on_voice = True
                    self.state.current_detection = "voice"
                    log.info("VOICE on %s (%.3f MHz)!", ch.name, ch.freq / 1e6)

                    # Hold on voice until it stops
                    self._hold_on_voice(ch)
                    self.state.paused_on_voice = False
            else:
                self.state.current_detection = "noise"

            # Move to next channel
            idx += 1
            if not self.state.paused_on_voice:
                time.sleep(max(0, self.state.scan_speed - 0.3))

    def _dwell_and_detect(self, ch: ChannelState, dwell_seconds: float = 2.0) -> bool:
        """Listen on a channel for dwell_seconds, return True if voice detected."""
        start = time.monotonic()
        last_log = 0.0
        while time.monotonic() - start < dwell_seconds and not self._stop_event.is_set():
            time.sleep(0.15)
            detection, confidence = self._process_audio()
            self.state.current_detection = detection.value
            self.state.current_confidence = confidence

            # Log detection progress periodically
            now = time.monotonic()
            if now - last_log > 0.5:
                audio_len = len(self._audio_buffer)
                window_len = len(self.detector.window)
                log.debug("  %s: det=%s conf=%.2f audio_buf=%d window=%d",
                         ch.name, detection.value, confidence, audio_len, window_len)
                last_log = now

            if detection == Detection.VOICE and confidence >= 0.3:
                return True
            if detection == Detection.DIGITAL and confidence >= 0.5:
                log.info("Digital signal on %s (conf=%.2f), skipping", ch.name, confidence)
                return False
        # Log final decision
        log.debug("  %s: dwell complete, final=%s conf=%.2f", ch.name, detection.value, confidence)
        return False

    def _hold_on_voice(self, ch: ChannelState):
        """Stay on frequency while voice is active, leave after hold_time of silence."""
        last_voice_time = time.monotonic()

        while not self._stop_event.is_set():
            time.sleep(0.2)
            detection, confidence = self._process_audio()
            self.state.current_detection = detection.value
            self.state.current_confidence = confidence

            if detection == Detection.VOICE:
                last_voice_time = time.monotonic()

            # Check S-meter too
            if self.state.current_smeter < self.state.squelch_level - 10:
                # Signal completely gone
                break

            if time.monotonic() - last_voice_time > self.state.voice_hold_time:
                log.info("Voice ended on %s, resuming scan", ch.name)
                break

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
                "group": ch.group,
                "skip": ch.skip,
                "last_signal": ch.last_signal.isoformat() if ch.last_signal else None,
                "last_voice": ch.last_voice.isoformat() if ch.last_voice else None,
                "signal_count": ch.signal_count,
                "voice_count": ch.voice_count,
                "smeter": ch.last_smeter,
                "active": ch.freq == self.state.current_freq,
            })

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
        }

    def shutdown(self):
        """Clean shutdown."""
        self.stop_scanning()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
