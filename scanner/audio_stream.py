"""Thread-safe audio broadcaster for streaming PCM audio to multiple WebSocket clients."""
from __future__ import annotations

import logging
import math
import queue
import struct
import threading

logger = logging.getLogger(__name__)


class AudioBroadcaster:
    """Broadcasts PCM audio to multiple WebSocket clients via per-client queues."""

    def __init__(self, sample_rate: int = 16000, max_clients: int = 10):
        self.sample_rate = sample_rate
        self.max_clients = max_clients
        self._clients: list[queue.Queue[bytes]] = []
        self._lock = threading.Lock()

    def push_audio(self, pcm_data: bytes) -> None:
        """Called by scanner thread with int16 PCM bytes. Distributes to all clients.
        Drops data for slow clients (non-blocking put with maxsize)."""
        with self._lock:
            for q in self._clients:
                try:
                    q.put_nowait(pcm_data)
                except queue.Full:
                    # Drop audio for slow clients rather than blocking
                    pass

    def subscribe(self) -> queue.Queue[bytes]:
        """Register a new client. Returns a Queue that will receive audio chunks."""
        with self._lock:
            if len(self._clients) >= self.max_clients:
                raise RuntimeError(
                    f"Max clients ({self.max_clients}) reached, cannot subscribe"
                )
            q: queue.Queue[bytes] = queue.Queue(maxsize=50)
            self._clients.append(q)
            logger.info("Audio client subscribed (total: %d)", len(self._clients))
            return q

    def unsubscribe(self, q: queue.Queue[bytes]) -> None:
        """Remove a client queue."""
        with self._lock:
            try:
                self._clients.remove(q)
                logger.info(
                    "Audio client unsubscribed (total: %d)", len(self._clients)
                )
            except ValueError:
                pass

    @property
    def client_count(self) -> int:
        """Number of connected audio clients."""
        with self._lock:
            return len(self._clients)

    def push_silence(self, duration_ms: int = 100) -> None:
        """Push silence (zeros) -- used between channels or when not on a signal."""
        num_samples = self.sample_rate * duration_ms // 1000
        silence = b"\x00\x00" * num_samples  # 2 bytes per int16 sample
        self.push_audio(silence)

    def push_tone(
        self, freq_hz: int = 1000, duration_ms: int = 100, volume: float = 0.3
    ) -> None:
        """Push a short tone -- used as channel ID beep when locking on a new channel.

        Generates a sine wave tone as int16 PCM bytes.
        """
        num_samples = self.sample_rate * duration_ms // 1000
        max_amplitude = 32767
        amplitude = max_amplitude * min(1.0, max(0.0, volume))

        samples: list[int] = []
        for i in range(num_samples):
            t = i / self.sample_rate
            value = int(amplitude * math.sin(2.0 * math.pi * freq_hz * t))
            samples.append(value)

        pcm_data = struct.pack(f"<{num_samples}h", *samples)
        self.push_audio(pcm_data)
