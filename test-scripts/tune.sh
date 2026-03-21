#!/usr/bin/env bash
# Tune to a frequency, record IQ samples, and print a capture summary.
set -euo pipefail

BOLD='\033[1m'
RESET='\033[0m'

usage() {
    printf "Usage: %s <frequency> [-s sample_rate] [-d duration] [-o output_file]\n" "$(basename "$0")"
    printf "\n"
    printf "  frequency    Tune frequency in Hz (required). Accepts SI suffixes: 100M, 433.92M, 1.09G\n"
    printf "  -s rate      Sample rate in Hz (default: 2048000)\n"
    printf "  -d seconds   Capture duration in seconds (default: 5)\n"
    printf "  -o file      Output file for raw IQ samples (default: /tmp/iq_capture.bin)\n"
    printf "\n"
    printf "Examples:\n"
    printf "  %s 433.92M\n" "$(basename "$0")"
    printf "  %s 100M -s 1024000 -d 10 -o /tmp/fm.bin\n" "$(basename "$0")"
}

# Positional frequency arg must come first
if [ $# -eq 0 ] || [[ "$1" == -* ]]; then
    usage
    exit 1
fi

FREQUENCY="$1"
shift

SAMPLE_RATE=2048000
DURATION=5
OUTPUT_FILE=/tmp/iq_capture.bin

while getopts ":s:d:o:" opt; do
    case "$opt" in
        s) SAMPLE_RATE="$OPTARG" ;;
        d) DURATION="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        :) printf "Option -%s requires an argument.\n" "$OPTARG" >&2; usage; exit 1 ;;
        \?) printf "Unknown option: -%s\n" "$OPTARG" >&2; usage; exit 1 ;;
    esac
done

SAMPLE_COUNT=$(( SAMPLE_RATE * DURATION ))

printf "${BOLD}Tuning to %s Hz — %ds at %s sps (%d samples)${RESET}\n" \
    "$FREQUENCY" "$DURATION" "$SAMPLE_RATE" "$SAMPLE_COUNT"
printf "Output: %s\n\n" "$OUTPUT_FILE"

rtl_sdr -f "$FREQUENCY" -s "$SAMPLE_RATE" -n "$SAMPLE_COUNT" "$OUTPUT_FILE"

FILE_SIZE=$(stat -c%s "$OUTPUT_FILE" 2>/dev/null || echo "unknown")

printf "\n${BOLD}Capture summary${RESET}\n"
printf "  Frequency:   %s Hz\n" "$FREQUENCY"
printf "  Sample rate: %s sps\n" "$SAMPLE_RATE"
printf "  Duration:    %s s\n" "$DURATION"
printf "  Samples:     %d\n" "$SAMPLE_COUNT"
printf "  File:        %s\n" "$OUTPUT_FILE"
printf "  Size:        %s bytes\n" "$FILE_SIZE"
