"""
Mobile-friendly web dashboard for the radio scanner.
Shows frequency list, signal activity, voice detection, skip list.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Flask, jsonify, request, Response

from .scanner import Scanner

log = logging.getLogger(__name__)

app = Flask(__name__)
scanner: Scanner | None = None


def create_app(scanner_instance: Scanner) -> Flask:
    global scanner
    scanner = scanner_instance
    return app


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Radio Scanner</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, system-ui, sans-serif;
  background: #111;
  color: #eee;
  font-size: 14px;
  -webkit-text-size-adjust: 100%;
}
.header {
  background: #1a1a2e;
  padding: 12px 16px;
  position: sticky;
  top: 0;
  z-index: 100;
  border-bottom: 2px solid #333;
}
.header h1 { font-size: 18px; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.sdr-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #a00;
}
.status-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: #aaa;
  margin-top: 4px;
}
.status-detail span { white-space: nowrap; }
#dwell-timer { color: #0f0; font-weight: bold; }
.status-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 13px;
}
.status-bar .freq { font-family: monospace; font-size: 16px; color: #0f0; font-weight: bold; }
.status-bar .smeter { color: #ff0; font-family: monospace; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: bold;
  text-transform: uppercase;
}
.badge-voice { background: #0a0; color: #fff; }
.badge-digital { background: #c50; color: #fff; }
.badge-noise { background: #444; color: #999; }
.badge-pending { background: #333; color: #666; }
.badge-scanning { background: #05a; color: #fff; animation: pulse 1s infinite; }
.badge-paused { background: #0a0; color: #fff; animation: pulse 0.5s infinite; }
@keyframes pulse { 50% { opacity: 0.6; } }

.controls {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  background: #1a1a1a;
  flex-wrap: wrap;
}
.controls button {
  padding: 8px 16px;
  border: 1px solid #444;
  border-radius: 6px;
  background: #222;
  color: #eee;
  font-size: 14px;
  cursor: pointer;
  flex: 1;
  min-width: 80px;
}
.controls button:active { background: #333; }
.controls button.active { background: #05a; border-color: #07c; }

.squelch-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: #1a1a1a;
  font-size: 13px;
}
.squelch-row input[type=range] { flex: 1; }
.squelch-row .val { font-family: monospace; min-width: 50px; }

.group-filter {
  display: flex;
  gap: 4px;
  padding: 8px 16px;
  overflow-x: auto;
  background: #151515;
}
.group-filter button {
  padding: 4px 10px;
  border: 1px solid #333;
  border-radius: 12px;
  background: #222;
  color: #aaa;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}
.group-filter button.active { background: #05a; color: #fff; border-color: #07c; }

.channel-list { padding: 0 0 80px 0; }
.channel {
  display: flex;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid #222;
  gap: 10px;
  transition: background 0.2s;
}
.channel.active { background: #1a2a1a; }
.channel.has-voice { border-left: 3px solid #0a0; }
.channel.has-signal { border-left: 3px solid #cc0; }
.channel.skipped { opacity: 0.4; }
.channel .freq-col {
  font-family: monospace;
  font-size: 13px;
  min-width: 90px;
  color: #0cf;
}
.channel .name-col {
  flex: 1;
  font-size: 13px;
}
.channel .name-col .group { color: #666; font-size: 11px; }
.channel .stats-col {
  text-align: right;
  font-size: 11px;
  color: #888;
  min-width: 60px;
}
.channel .stats-col .voice-count { color: #0a0; }
.channel .skip-btn {
  padding: 4px 8px;
  border: 1px solid #333;
  border-radius: 4px;
  background: #222;
  color: #aaa;
  font-size: 11px;
  cursor: pointer;
}
.channel .skip-btn.skipped { background: #400; color: #f88; border-color: #600; }
.smeter-bar {
  width: 40px;
  height: 8px;
  background: #333;
  border-radius: 4px;
  overflow: hidden;
}
.smeter-bar .fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.3s;
}
.activity-section { padding: 0 16px 8px; }
.activity-toggle {
  width: 100%;
  padding: 6px;
  border: 1px solid #333;
  border-radius: 6px;
  background: #1a1a1a;
  color: #aaa;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.activity-toggle:active { background: #222; }
.activity-log {
  max-height: 200px;
  overflow-y: auto;
  background: #0a0a0a;
  border: 1px solid #222;
  border-radius: 4px;
  margin-top: 4px;
  padding: 4px;
  font-family: monospace;
  font-size: 11px;
}
.activity-entry {
  padding: 2px 6px;
  border-bottom: 1px solid #1a1a1a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.act-voice { color: #0f0; }
.act-signal { color: #cc0; }
</style>
</head>
<body>
<div class="header">
  <h1><span class="sdr-dot" id="sdr-dot"></span>Radio Scanner</h1>
  <div class="status-bar">
    <span class="freq" id="current-freq">---</span>
    <span id="current-name"></span>
    <span class="smeter" id="current-smeter">S--</span>
    <span class="badge badge-pending" id="detection-badge">--</span>
    <span class="badge" id="scan-badge">--</span>
  </div>
  <div class="status-detail">
    <span id="current-band"></span>
    <span id="scan-progress"></span>
    <span id="cycle-time"></span>
    <span id="dwell-timer" style="display:none"></span>
  </div>
</div>

<div class="controls">
  <button id="btn-scan" onclick="toggleScan()">Start Scan</button>
  <button id="btn-stop" onclick="stopScan()">Stop</button>
</div>

<div class="squelch-row">
  <label>Squelch:</label>
  <input type="range" id="squelch" min="-100" max="-20" value="-60" step="1"
    oninput="setSquelch(this.value)">
  <span class="val" id="squelch-val">-60 dB</span>
</div>

<div class="group-filter" id="group-filter"></div>

<div class="activity-section">
  <button class="activity-toggle" onclick="toggleActivity()">Activity Log</button>
  <div class="activity-log" id="activity-log" style="display:none"></div>
</div>

<div class="channel-list" id="channel-list"></div>

<script>
let state = {};
let activeGroup = "all";
let pollInterval;

async function api(path, opts) {
  const r = await fetch('/api' + path, opts);
  return r.json();
}

async function toggleScan() {
  await api('/scan/start', {method: 'POST'});
  poll();
}
async function stopScan() {
  await api('/scan/stop', {method: 'POST'});
  poll();
}
async function setSquelch(val) {
  document.getElementById('squelch-val').textContent = val + ' dB';
  await api('/squelch', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({level: parseFloat(val)})});
}
async function toggleSkip(freq) {
  await api('/skip/' + freq, {method: 'POST'});
  poll();
}

function filterGroup(group) {
  activeGroup = group;
  document.querySelectorAll('.group-filter button').forEach(b => {
    b.classList.toggle('active', b.dataset.group === group);
  });
  renderChannels();
}

function smeterColor(db) {
  if (db > -40) return '#0f0';
  if (db > -60) return '#cc0';
  if (db > -80) return '#f80';
  return '#444';
}
function smeterWidth(db) {
  // Map -120..-20 to 0..100%
  return Math.max(0, Math.min(100, (db + 120) * 100 / 100));
}

function renderChannels() {
  const list = document.getElementById('channel-list');
  if (!state.channels) return;

  let channels = state.channels;
  if (activeGroup !== 'all') {
    channels = channels.filter(c => c.group === activeGroup);
  }

  list.innerHTML = channels.map(ch => {
    const cls = ['channel'];
    if (ch.active) cls.push('active');
    if (ch.voice_count > 0) cls.push('has-voice');
    else if (ch.signal_count > 0) cls.push('has-signal');
    if (ch.skip) cls.push('skipped');
    const fill = smeterWidth(ch.smeter);
    const color = smeterColor(ch.smeter);
    return `<div class="${cls.join(' ')}">
      <div class="freq-col">${ch.freq_mhz}</div>
      <div class="name-col">${ch.name}<br><span class="group">${ch.group}</span></div>
      <div class="smeter-bar"><div class="fill" style="width:${fill}%;background:${color}"></div></div>
      <div class="stats-col">
        ${ch.signal_count > 0 ? ch.signal_count + ' sig' : ''}
        ${ch.voice_count > 0 ? '<br><span class="voice-count">' + ch.voice_count + ' voice</span>' : ''}
      </div>
      <button class="skip-btn ${ch.skip ? 'skipped' : ''}" onclick="toggleSkip(${ch.freq})">
        ${ch.skip ? 'SKIP' : 'skip'}
      </button>
    </div>`;
  }).join('');
}

function renderGroups() {
  const groups = [...new Set((state.channels || []).map(c => c.group))];
  const container = document.getElementById('group-filter');
  container.innerHTML = `<button class="active" data-group="all" onclick="filterGroup('all')">All</button>` +
    groups.map(g => `<button data-group="${g}" onclick="filterGroup('${g}')">${g}</button>`).join('');
}

function updateHeader() {
  document.getElementById('current-freq').textContent =
    state.current_freq_mhz ? state.current_freq_mhz + ' MHz' : '---';
  document.getElementById('current-name').textContent = state.current_channel || '';
  document.getElementById('current-smeter').textContent =
    state.current_smeter ? 'S ' + state.current_smeter.toFixed(0) + ' dB' : 'S--';

  const det = document.getElementById('detection-badge');
  const d = state.current_detection || 'pending';
  det.textContent = d;
  det.className = 'badge badge-' + d;

  const scan = document.getElementById('scan-badge');
  if (state.paused_on_voice) {
    scan.textContent = 'VOICE LOCKED';
    scan.className = 'badge badge-paused';
  } else if (state.scanning) {
    scan.textContent = 'SCANNING';
    scan.className = 'badge badge-scanning';
  } else {
    scan.textContent = 'STOPPED';
    scan.className = 'badge badge-noise';
  }

  document.getElementById('squelch').value = state.squelch_level || -60;
  document.getElementById('squelch-val').textContent = (state.squelch_level || -60) + ' dB';

  // SDR dot
  const dot = document.getElementById('sdr-dot');
  dot.style.background = state.sdr_connected ? '#0a0' : '#a00';

  // Band
  document.getElementById('current-band').textContent =
    state.current_band ? 'Band: ' + state.current_band : '';

  // Progress
  document.getElementById('scan-progress').textContent =
    state.scanning ? (state.scan_index || 0) + '/' + (state.scan_total || 0) + ' channels' : '';

  // Cycle time
  document.getElementById('cycle-time').textContent =
    state.scan_cycle_time ? 'Cycle: ' + state.scan_cycle_time.toFixed(1) + 's' : '';

  // Dwell timer
  const dwell = document.getElementById('dwell-timer');
  if (state.paused_on_voice && state.channel_dwell_elapsed) {
    dwell.textContent = 'Holding: ' + state.channel_dwell_elapsed.toFixed(1) + 's';
    dwell.style.display = '';
  } else {
    dwell.style.display = 'none';
  }
}

function toggleActivity() {
  const el = document.getElementById('activity-log');
  el.style.display = el.style.display === 'none' ? '' : 'none';
  if (el.style.display !== 'none') renderActivity();
}

function renderActivity() {
  const log = state.activity_log || [];
  const el = document.getElementById('activity-log');
  if (!el || el.style.display === 'none') return;
  el.innerHTML = log.slice().reverse().map(e => {
    const ts = e.ts ? e.ts.split('T')[1].split('.')[0] : '';
    const cls = e.type === 'voice' ? 'act-voice' : 'act-signal';
    const freq = (e.freq / 1e6).toFixed(4);
    const detail = e.type === 'voice' ?
      (e.duration ? e.duration.toFixed(1) + 's' : '') :
      (e.power ? e.power + ' dB' : '');
    return `<div class="activity-entry ${cls}">${ts} ${e.type.toUpperCase()} ${e.channel} ${freq} MHz ${detail}</div>`;
  }).join('');
}

async function poll() {
  try {
    state = await api('/state');
    updateHeader();
    renderChannels();
    renderActivity();
    // Only render groups on first load
    if (!document.querySelector('.group-filter button')) renderGroups();
  } catch(e) {
    console.error('Poll error:', e);
  }
}

// Poll every 500ms
poll();
pollInterval = setInterval(poll, 500);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, content_type="text/html")


@app.route("/api/state")
def get_state():
    return jsonify(scanner.get_state_dict())


@app.route("/api/scan/start", methods=["POST"])
def start_scan():
    scanner.start_scanning()
    return jsonify({"ok": True})


@app.route("/api/scan/stop", methods=["POST"])
def stop_scan():
    scanner.stop_scanning()
    return jsonify({"ok": True})


@app.route("/api/skip/<int:freq>", methods=["POST"])
def toggle_skip(freq: int):
    new_state = scanner.toggle_skip(freq)
    return jsonify({"ok": True, "skip": new_state})


@app.route("/api/activity")
def get_activity():
    return jsonify(list(scanner.state.activity_log))


@app.route("/api/squelch", methods=["POST"])
def set_squelch():
    data = request.get_json()
    scanner.set_squelch(data["level"])
    return jsonify({"ok": True})
