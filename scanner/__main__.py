"""
Entry point: python -m scanner [--driver DRIVER] [--gain GAIN] [--web-port WEB_PORT]
"""

import argparse
import logging
import signal
import sys

from .sdr_backend import SoapySdrBackend
from .audio_stream import AudioBroadcaster
from .scanner import Scanner
from .web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")


def main():
    parser = argparse.ArgumentParser(description="Radio Scanner")
    parser.add_argument("--driver", default="rtlsdr", help="SoapySDR driver (default: rtlsdr)")
    parser.add_argument("--gain", type=float, default=None, help="SDR gain in dB (default: auto)")
    parser.add_argument("--iq-file", default=None, help="IQ file for testing without hardware (uses FileSdrBackend)")
    parser.add_argument("--web-port", type=int, default=8080, help="Web dashboard port (default: 8080)")
    parser.add_argument("--squelch", type=float, default=-45, help="Squelch level in dB (default: -45)")
    parser.add_argument("--auto-start", action="store_true", help="Start scanning immediately")
    parser.add_argument("--record", action="store_true", help="Enable voice recording to WAV files")
    parser.add_argument("--record-dir", default="recordings", help="Directory for recordings (default: recordings)")
    args = parser.parse_args()

    # Create SDR backend
    if args.iq_file:
        from .sdr_backend import FileSdrBackend
        backend = FileSdrBackend(args.iq_file, sample_rate=2_400_000, center_freq=0)
    else:
        backend = SoapySdrBackend(driver=args.driver, gain=args.gain)

    # Create audio broadcaster
    broadcaster = AudioBroadcaster()

    # Create voice recorder if enabled
    recorder = None
    if args.record:
        from .recorder import VoiceRecorder
        recorder = VoiceRecorder(output_dir=args.record_dir)

    scanner = Scanner(backend=backend, broadcaster=broadcaster, recorder=recorder)
    scanner.set_squelch(args.squelch)

    # Open SDR backend with retry
    import time
    max_retries = 30
    for attempt in range(max_retries):
        try:
            backend.open()
            break
        except Exception as e:
            log.error("Failed to open SDR backend: %s", e)
            if attempt < max_retries - 1:
                wait = min(2 * (attempt + 1), 10)
                log.info("Retrying in %ds (attempt %d/%d)...", wait, attempt + 1, max_retries)
                time.sleep(wait)
            else:
                log.error("Giving up after %d attempts", max_retries)
                # Start web dashboard anyway so user can see the error
                break

    if args.auto_start and backend.is_open():
        scanner.start_scanning()

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down...")
        scanner.shutdown()
        backend.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start web dashboard
    app = create_app(scanner, broadcaster)
    log.info("Dashboard at http://0.0.0.0:%d", args.web_port)
    app.run(host="0.0.0.0", port=args.web_port, threaded=True)


if __name__ == "__main__":
    main()
