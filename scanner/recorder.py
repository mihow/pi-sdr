"""Simple WAV file recorder for detected voice transmissions."""
from __future__ import annotations

import logging
import wave
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class VoiceRecorder:
    """Records detected voice transmissions to WAV files."""

    def __init__(self, output_dir: str = "recordings", sample_rate: int = 16000):
        self.output_dir = Path(output_dir)
        self.sample_rate = sample_rate
        self._current_file: wave.Wave_write | None = None
        self._current_path: Path | None = None

    def start_recording(self, channel_name: str, freq: int) -> Path:
        """Start a new recording. Returns the file path.

        Filename format: GMRS-7_462.7125_2026-03-23_12-00-05.wav
        """
        if self._current_file is not None:
            logger.warning("Recording already in progress, stopping previous")
            self.stop_recording()

        self.output_dir.mkdir(parents=True, exist_ok=True)

        freq_mhz = freq / 1_000_000
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{channel_name}_{freq_mhz:.4f}_{timestamp}.wav"
        path = self.output_dir / filename

        self._current_file = wave.open(str(path), "wb")
        self._current_file.setnchannels(1)
        self._current_file.setsampwidth(2)  # 16-bit
        self._current_file.setframerate(self.sample_rate)
        self._current_path = path

        logger.info("Started recording: %s", path)
        return path

    def write_audio(self, pcm_data: bytes) -> None:
        """Write PCM audio data to the current recording."""
        if self._current_file is None:
            return
        self._current_file.writeframes(pcm_data)

    def stop_recording(self) -> Path | None:
        """Stop the current recording. Returns the path of the completed file."""
        if self._current_file is None:
            return None

        path = self._current_path
        try:
            self._current_file.close()
            logger.info("Stopped recording: %s", path)
        except Exception:
            logger.exception("Error closing recording file")
        finally:
            self._current_file = None
            self._current_path = None

        return path

    @property
    def is_recording(self) -> bool:
        """Whether a recording is in progress."""
        return self._current_file is not None
