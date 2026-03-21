#!/usr/bin/env bash
# Check RTL-SDR device presence, version, and driver status.
# Exit 0 if a working RTL-SDR is found, 1 otherwise.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { printf "${GREEN}%-6s${RESET} %s\n" "[PASS]" "$1"; }
fail() { printf "${RED}%-6s${RESET} %s\n" "[FAIL]" "$1"; }
warn() { printf "${YELLOW}%-6s${RESET} %s\n" "[WARN]" "$1"; }
info() { printf "${BOLD}%-6s${RESET} %s\n" "[INFO]" "$1"; }

errors=0

# --- USB device ---
echo ""
info "USB device"

usb_line=$(lsusb 2>/dev/null | grep -i "0bda:2838\|RTL2838\|RTL2832" || true)
if [ -n "$usb_line" ]; then
    pass "RTL-SDR USB device found: $usb_line"
else
    fail "No RTL-SDR USB device found (expected Realtek 0bda:2838)"
    echo "       Is the dongle plugged in?"
    errors=$((errors + 1))
fi

# --- Kernel modules ---
echo ""
info "Kernel modules"

for mod in dvb_usb_rtl28xxu rtl2832 rtl2832_sdr; do
    if lsmod | grep -q "^${mod}"; then
        warn "Kernel module '$mod' is loaded — will conflict with userspace rtl-sdr"
        echo "       Run: sudo rmmod $mod"
        errors=$((errors + 1))
    fi
done

blacklist_found=false
for f in /etc/modprobe.d/*sdr* /etc/modprobe.d/*dvb* /etc/modprobe.d/*rtl*; do
    if [ -f "$f" ] 2>/dev/null; then
        blacklist_found=true
        pass "Blacklist file found: $f"
    fi
done
if ! $blacklist_found; then
    warn "No DVB blacklist file in /etc/modprobe.d/ (ok on host, needed on Pi)"
fi

# --- rtl-sdr tools ---
echo ""
info "rtl-sdr"

if command -v rtl_test &>/dev/null; then
    rtl_path=$(command -v rtl_test)
    # Try to get version from package manager, fall back to binary path
    if dpkg -l rtl-sdr &>/dev/null 2>&1; then
        rtl_ver=$(dpkg-query -W -f='${Version}' rtl-sdr 2>/dev/null)
        pass "rtl-sdr installed: $rtl_ver ($rtl_path)"
    elif rpm -q rtl-sdr &>/dev/null 2>&1; then
        rtl_ver=$(rpm -q --qf '%{VERSION}' rtl-sdr 2>/dev/null)
        pass "rtl-sdr installed: $rtl_ver ($rtl_path)"
    else
        pass "rtl_test found: $rtl_path (version unknown — not from package manager)"
    fi
else
    fail "rtl_test not found"
    errors=$((errors + 1))
fi

# --- rtl_test device probe ---
echo ""
info "Device probe (rtl_test)"

if command -v rtl_test &>/dev/null && [ -n "$usb_line" ]; then
    # rtl_test -t exits non-zero when no E4000 tuner is found, which is normal for V4
    probe_output=$(rtl_test -t 2>&1 | head -10 || true)

    if echo "$probe_output" | grep -qi "Blog V4"; then
        pass "RTL-SDR Blog V4 detected"
    elif echo "$probe_output" | grep -qi "R828D\|R820T"; then
        pass "RTL-SDR detected (tuner: $(echo "$probe_output" | grep -oi 'R[0-9]*[A-Z]*' | head -1))"
    elif echo "$probe_output" | grep -qi "No supported devices found\|usb_claim_interface error"; then
        fail "rtl_test cannot access device (permissions or driver conflict)"
        echo "       Try: sudo rmmod dvb_usb_rtl28xxu"
        errors=$((errors + 1))
    else
        warn "rtl_test ran but could not identify the device"
        echo "$probe_output" | sed 's/^/       /'
    fi

    # Report gain values as a sanity check
    gains=$(echo "$probe_output" | grep -o 'Supported gain values.*' || true)
    if [ -n "$gains" ]; then
        info "  $gains"
    fi
else
    warn "Skipping device probe (rtl_test not available or no USB device)"
fi

# --- SoapySDR ---
echo ""
info "SoapySDR"

if command -v SoapySDRUtil &>/dev/null; then
    soapy_ver=$(SoapySDRUtil --info 2>&1 | grep -oP 'Soapy SDR.*?v\K[\d.]+' || echo "unknown")
    pass "SoapySDR installed: v$soapy_ver ($(command -v SoapySDRUtil))"

    # Check for rtlsdr module
    if SoapySDRUtil --find 2>&1 | grep -q "driver = rtlsdr"; then
        sdr_info=$(SoapySDRUtil --find 2>&1 | grep -A6 "driver = rtlsdr")
        product=$(echo "$sdr_info" | grep "product" | cut -d= -f2 | xargs)
        manufacturer=$(echo "$sdr_info" | grep "manufacturer" | cut -d= -f2 | xargs)
        tuner=$(echo "$sdr_info" | grep "tuner" | cut -d= -f2 | xargs)
        pass "SoapySDR finds RTL-SDR: $manufacturer $product ($tuner)"
    else
        if [ -n "$usb_line" ]; then
            fail "SoapySDR cannot find RTL-SDR device"
            errors=$((errors + 1))
        else
            warn "SoapySDR rtlsdr module present but no device to detect"
        fi
    fi
else
    warn "SoapySDRUtil not found (optional — needed for SoapySDR integration)"
fi

# --- Python bindings ---
echo ""
info "Python bindings"

if python3 -c "import SoapySDR; print(f'SoapySDR {SoapySDR.getAPIVersion()}')" 2>/dev/null; then
    pass "python3-soapysdr works"
else
    warn "python3-soapysdr not available (optional)"
fi

# --- Summary ---
echo ""
if [ "$errors" -eq 0 ]; then
    printf "${GREEN}${BOLD}All checks passed.${RESET}\n"
else
    printf "${RED}${BOLD}%d check(s) failed.${RESET}\n" "$errors"
fi
echo ""
exit "$errors"
