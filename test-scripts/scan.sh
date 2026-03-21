#!/usr/bin/env bash
# Quick scan across a frequency range using rtl_power.
set -euo pipefail

BOLD='\033[1m'
RESET='\033[0m'

usage() {
    printf "Usage: %s <start_freq> <end_freq> [-b bin_size] [-i interval] [-n num_sweeps] [-o output_file]\n" "$(basename "$0")"
    printf "\n"
    printf "  start_freq   Start of scan range (required). Accepts SI suffixes: 80M, 1.09G\n"
    printf "  end_freq     End of scan range (required).\n"
    printf "  -b size      Bin size / frequency resolution (default: 1M)\n"
    printf "  -i seconds   Integration interval per sweep in seconds (default: 1)\n"
    printf "  -n sweeps    Number of sweeps to perform (default: 1)\n"
    printf "  -o file      Output file for CSV data (default: stdout)\n"
    printf "\n"
    printf "Examples:\n"
    printf "  %s 80M 108M\n" "$(basename "$0")"
    printf "  %s 400M 500M -b 500k -n 5 -o /tmp/scan.csv\n" "$(basename "$0")"
}

# Both positional args required
if [ $# -lt 2 ] || [[ "$1" == -* ]] || [[ "$2" == -* ]]; then
    usage
    exit 1
fi

START_FREQ="$1"
END_FREQ="$2"
shift 2

BIN_SIZE=1M
INTERVAL=1
NUM_SWEEPS=1
OUTPUT_FILE=/dev/stdout

while getopts ":b:i:n:o:" opt; do
    case "$opt" in
        b) BIN_SIZE="$OPTARG" ;;
        i) INTERVAL="$OPTARG" ;;
        n) NUM_SWEEPS="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        :) printf "Option -%s requires an argument.\n" "$OPTARG" >&2; usage; exit 1 ;;
        \?) printf "Unknown option: -%s\n" "$OPTARG" >&2; usage; exit 1 ;;
    esac
done

TOTAL_TIME=$(( INTERVAL * NUM_SWEEPS ))

printf "${BOLD}Scanning %s – %s (bin: %s, %d sweep(s))${RESET}\n" \
    "$START_FREQ" "$END_FREQ" "$BIN_SIZE" "$NUM_SWEEPS" >&2
printf "Output: %s\n\n" "$OUTPUT_FILE" >&2

rtl_power -f "${START_FREQ}:${END_FREQ}:${BIN_SIZE}" \
    -i "$INTERVAL" \
    -e "$TOTAL_TIME" \
    "$OUTPUT_FILE"

printf "\n${BOLD}Scan summary${RESET}\n" >&2
printf "  Range:     %s – %s\n" "$START_FREQ" "$END_FREQ" >&2
printf "  Bin size:  %s\n" "$BIN_SIZE" >&2
printf "  Sweeps:    %d\n" "$NUM_SWEEPS" >&2
printf "  Output:    %s\n" "$OUTPUT_FILE" >&2
