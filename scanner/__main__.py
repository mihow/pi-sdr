"""
Entry point: python -m scanner [--host OWRX_HOST] [--port OWRX_PORT] [--web-port WEB_PORT]
"""

import argparse
import logging
import signal
import sys

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
    parser.add_argument("--host", default="localhost", help="OpenWebRX+ host (default: localhost)")
    parser.add_argument("--port", type=int, default=8073, help="OpenWebRX+ port (default: 8073)")
    parser.add_argument("--web-port", type=int, default=8080, help="Web dashboard port (default: 8080)")
    parser.add_argument("--ssl", action="store_true", default=True, help="Use SSL/WSS (default: true)")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    parser.add_argument("--squelch", type=float, default=-45, help="Squelch level in dB (default: -45)")
    parser.add_argument("--auto-start", action="store_true", help="Start scanning immediately")
    args = parser.parse_args()

    use_ssl = args.ssl and not args.no_ssl
    scanner = Scanner(owrx_host=args.host, owrx_port=args.port, use_ssl=use_ssl)
    scanner.set_squelch(args.squelch)

    # Connect to OpenWebRX+
    try:
        scanner.connect()
    except Exception as e:
        log.error("Failed to connect to OpenWebRX+ at %s:%d: %s", args.host, args.port, e)
        log.error("Make sure OpenWebRX+ is running and accessible")
        sys.exit(1)

    if args.auto_start:
        scanner.start_scanning()

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down...")
        scanner.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start web dashboard
    app = create_app(scanner)
    log.info("Dashboard at http://0.0.0.0:%d", args.web_port)
    app.run(host="0.0.0.0", port=args.web_port, threaded=True)


if __name__ == "__main__":
    main()
