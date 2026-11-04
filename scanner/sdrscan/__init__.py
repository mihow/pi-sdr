"""sdrscan: an unattended RTL-SDR scanner that summarizes channel activity on device.

Two sample rates hold for the whole package. ``IQ_RATE`` is the tuner rate. ``AUDIO_RATE``
is the one rate every stage after demodulation reads (voice gate, clips, speech-to-text),
so a rate mismatch between stages cannot happen.
"""

AUDIO_RATE = 16_000
IQ_RATE = 2_400_000
__version__ = "0.1.0"
