"""SDR hardware abstraction layer.

Provides a protocol for SDR backends and a concrete SoapySDR implementation.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import numpy as np

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
except ImportError:
    SoapySDR = None

logger = logging.getLogger(__name__)


@runtime_checkable
class SdrBackend(Protocol):
    """Protocol for SDR hardware backends."""

    def open(self) -> None:
        """Open the SDR device and start streaming."""
        ...

    def close(self) -> None:
        """Stop streaming and close the device."""
        ...

    def tune(self, center_freq: int) -> None:
        """Set the center frequency in Hz."""
        ...

    def read_iq(self, num_samples: int = 262144) -> np.ndarray:
        """Read IQ samples from the device.

        Returns a numpy array of complex64 values.
        """
        ...

    def get_center_freq(self) -> int:
        """Return the current center frequency in Hz."""
        ...

    def get_sample_rate(self) -> int:
        """Return the sample rate in Hz."""
        ...

    def get_max_bandwidth(self) -> int:
        """Return the maximum usable bandwidth in Hz."""
        ...

    def is_open(self) -> bool:
        """Return True if the device is open and streaming."""
        ...


class SoapySdrBackend:
    """SoapySDR-based SDR backend.

    Supports RTL-SDR V4 (default) and other SoapySDR-compatible devices.
    """

    def __init__(
        self,
        driver: str = "rtlsdr",
        gain: float | None = None,
        sample_rate: int = 2_400_000,
    ) -> None:
        if SoapySDR is None:
            raise ImportError(
                "SoapySDR Python bindings not installed. "
                "Install with: apt install python3-soapysdr soapysdr-module-rtlsdr"
            )
        self._driver = driver
        self._gain = gain  # None = auto-gain
        self._sample_rate = sample_rate
        self._device: SoapySDR.Device | None = None
        self._stream = None
        self._center_freq: int = 0
        self._is_open = False

    def open(self) -> None:
        """Open the SDR device and start streaming."""
        if self._is_open:
            logger.warning("Device already open")
            return

        logger.info("Opening SoapySDR device (driver=%s)", self._driver)
        self._device = SoapySDR.Device({"driver": self._driver})

        # Set sample rate
        self._device.setSampleRate(SOAPY_SDR_RX, 0, self._sample_rate)
        actual_rate = self._device.getSampleRate(SOAPY_SDR_RX, 0)
        logger.info("Sample rate: requested=%d, actual=%d", self._sample_rate, actual_rate)

        # Set gain
        if self._gain is None:
            self._device.setGainMode(SOAPY_SDR_RX, 0, True)
            logger.info("Gain: auto")
        else:
            self._device.setGainMode(SOAPY_SDR_RX, 0, False)
            self._device.setGain(SOAPY_SDR_RX, 0, self._gain)
            actual_gain = self._device.getGain(SOAPY_SDR_RX, 0)
            logger.info("Gain: requested=%.1f dB, actual=%.1f dB", self._gain, actual_gain)

        # Set up and activate stream
        self._stream = self._device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self._device.activateStream(self._stream)
        self._is_open = True
        logger.info("SDR device opened and streaming")

    def close(self) -> None:
        """Stop streaming and close the device."""
        if not self._is_open:
            return

        logger.info("Closing SDR device")
        if self._stream is not None and self._device is not None:
            self._device.deactivateStream(self._stream)
            self._device.closeStream(self._stream)
            self._stream = None
        self._device = None
        self._is_open = False
        logger.info("SDR device closed")

    def tune(self, center_freq: int) -> None:
        """Set the center frequency in Hz."""
        if not self._is_open or self._device is None:
            raise RuntimeError("Device not open")

        self._device.setFrequency(SOAPY_SDR_RX, 0, float(center_freq))
        self._center_freq = center_freq
        logger.debug("Tuned to %d Hz (%.4f MHz)", center_freq, center_freq / 1e6)

    def read_iq(self, num_samples: int = 262144) -> np.ndarray:
        """Read IQ samples from the device.

        Reads in a loop until num_samples are collected.
        Returns a numpy array of complex64 values.
        """
        if not self._is_open or self._device is None or self._stream is None:
            raise RuntimeError("Device not open")

        buf = np.zeros(num_samples, dtype=np.complex64)
        samples_read = 0

        while samples_read < num_samples:
            remaining = num_samples - samples_read
            chunk = np.zeros(remaining, dtype=np.complex64)
            sr = self._device.readStream(self._stream, [chunk], remaining)

            if sr.ret > 0:
                buf[samples_read : samples_read + sr.ret] = chunk[: sr.ret]
                samples_read += sr.ret
            elif sr.ret == SoapySDR.SOAPY_SDR_TIMEOUT:
                logger.warning("readStream timeout, retrying")
                continue
            elif sr.ret == SoapySDR.SOAPY_SDR_OVERFLOW:
                logger.warning("readStream overflow, samples lost")
                continue
            else:
                raise RuntimeError(f"readStream error: {sr.ret}")

        return buf

    def get_center_freq(self) -> int:
        """Return the current center frequency in Hz."""
        return self._center_freq

    def get_sample_rate(self) -> int:
        """Return the sample rate in Hz."""
        return self._sample_rate

    def get_max_bandwidth(self) -> int:
        """Return the maximum usable bandwidth in Hz.

        For RTL-SDR, this is 2.4 MHz. The edges of the bandwidth
        tend to roll off, so the usable bandwidth is the sample rate.
        """
        return self._sample_rate

    def is_open(self) -> bool:
        """Return True if the device is open and streaming."""
        return self._is_open
