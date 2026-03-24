"""
Mobile-friendly web dashboard for the radio scanner.
Shows frequency list, signal activity, voice detection, skip list.
"""

from __future__ import annotations

import json
import logging
import queue
import struct
from pathlib import Path

from flask import Flask, jsonify, request, Response
from flask_sock import Sock

from .audio_stream import AudioBroadcaster
from .scanner import Scanner

log = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock()
scanner: Scanner | None = None
broadcaster: AudioBroadcaster | None = None


def create_app(scanner_instance: Scanner, broadcaster_instance: AudioBroadcaster | None = None) -> Flask:
    global scanner, broadcaster
    scanner = scanner_instance
    broadcaster = broadcaster_instance
    sock.init_app(app)
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

.audio-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: #1a1a1a;
  border-bottom: 1px solid #333;
  font-size: 13px;
}
.audio-controls button {
  padding: 6px 12px;
  border: 1px solid #444;
  border-radius: 6px;
  background: #222;
  color: #eee;
  font-size: 13px;
  cursor: pointer;
}
.audio-controls button.active { background: #05a; border-color: #07c; }
.audio-controls input[type=range] { flex: 1; max-width: 120px; }
.audio-controls .val { font-family: monospace; min-width: 35px; }

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

.sort-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 16px;
  background: #151515;
  font-size: 12px;
}
.sort-row select {
  background: #222;
  color: #eee;
  border: 1px solid #444;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 12px;
}
.badge-listening { background: #a0a; color: #fff; animation: pulse 1s infinite; }
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
.sdr-source { font-family: monospace; font-size: 11px; color: #666; margin-left: 8px; }
.info-btn {
  background: none;
  border: 1px solid #444;
  border-radius: 50%;
  color: #888;
  font-size: 12px;
  width: 20px;
  height: 20px;
  cursor: pointer;
  padding: 0;
  line-height: 18px;
}
.info-btn:hover { color: #fff; border-color: #888; }
.band-overview {
  padding: 8px 16px;
  background: #151515;
  border-bottom: 1px solid #222;
}
.band-card {
  display: inline-block;
  padding: 6px 12px;
  margin: 4px;
  border-radius: 6px;
  background: #222;
  border: 1px solid #333;
  font-size: 12px;
  vertical-align: top;
}
.band-card.active { border-color: #05a; background: #1a1a3e; }
.band-card .band-name { font-weight: bold; color: #0cf; }
.band-card .band-range { color: #888; font-family: monospace; font-size: 11px; }
.band-card .band-signals { color: #0a0; font-size: 11px; }
.ch-name { cursor: pointer; }
.ch-name:hover { text-decoration: underline; color: #fff; }
#scan-window { font-family: monospace; color: #888; }
.fft-section {
  padding: 8px 16px;
  background: #0a0a0a;
  border-bottom: 1px solid #222;
}
#fft-canvas {
  width: 100%;
  height: 120px;
  display: block;
}
</style>
</head>
<body>
<div class="header">
  <h1><span class="sdr-dot" id="sdr-dot"></span>Radio Scanner<span id="sdr-source" class="sdr-source"></span><span id="conn-status" style="font-size:11px;margin-left:8px"></span><button class="info-btn" onclick="showSdrInfo()" title="SDR Info">&#8505;</button></h1>
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
    <span id="scan-window"></span>
    <span id="dwell-timer" style="display:none"></span>
  </div>
</div>

<div class="controls">
  <button id="btn-scan" onclick="toggleScan()">Start Scan</button>
  <button id="btn-stop" onclick="stopScan()">Stop</button>
  <button id="btn-discover" onclick="toggleAutoDiscover()">Auto-Discover</button>
</div>

<div class="squelch-row">
  <label>Squelch:</label>
  <input type="range" id="squelch" min="-100" max="-20" value="-60" step="1"
    oninput="setSquelch(this.value)">
  <span class="val" id="squelch-val">-60 dB</span>
</div>

<div class="audio-controls">
  <button id="audio-btn" onclick="toggleAudio()">Listen</button>
  <button id="mute-btn" onclick="toggleMute()">Mute</button>
  <label>Vol:</label>
  <input type="range" id="volume" min="0" max="1" value="0.7" step="0.05"
    oninput="setAudioVolume(this.value)">
  <span class="val" id="vol-val">70%</span>
</div>

<div class="band-overview" id="band-overview"></div>

<div class="fft-section">
  <canvas id="fft-canvas" width="800" height="120"></canvas>
</div>

<div class="group-filter" id="group-filter"></div>

<div class="sort-row">
  <label>Sort:</label>
  <select id="sort-select" onchange="sortChannels(this.value)">
    <option value="freq">Frequency</option>
    <option value="recent">Recent Activity</option>
    <option value="signal">Most Signals</option>
    <option value="voice">Most Voice</option>
    <option value="name">Name</option>
  </select>
</div>

<div class="activity-section">
  <button class="activity-toggle" onclick="toggleActivity()">Activity Log</button>
  <div class="activity-log" id="activity-log" style="display:none"></div>
</div>

<div class="channel-list" id="channel-list"></div>

<script>
let state = {};
let activeGroup = "all";
let sortMode = "freq";
let pollInterval;
let lastPollOk = false;
let pollFailCount = 0;

let audioCtx = null;
let audioWs = null;
let audioQueue = [];
let audioPlaying = false;
let audioMuted = false;
let audioVolume = 0.7;
let audioNode = null;

function toggleAudio() {
  if (audioPlaying) {
    stopAudio();
  } else {
    startAudio();
  }
}

function startAudio() {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 16000});
  const gainNode = audioCtx.createGain();
  gainNode.gain.value = audioVolume;
  gainNode.connect(audioCtx.destination);

  // ScriptProcessorNode for PCM playback
  audioNode = audioCtx.createScriptProcessor(2048, 0, 1);
  audioNode.onaudioprocess = (e) => {
    const output = e.outputBuffer.getChannelData(0);
    if (audioQueue.length > 0 && !audioMuted) {
      const chunk = audioQueue.shift();
      const samples = new Int16Array(chunk);
      for (let i = 0; i < output.length; i++) {
        output[i] = i < samples.length ? samples[i] / 32768.0 : 0;
      }
    } else {
      output.fill(0);
    }
  };
  audioNode.connect(gainNode);

  // WebSocket connection
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  audioWs = new WebSocket(proto + '//' + location.host + '/ws/audio');
  audioWs.binaryType = 'arraybuffer';
  audioWs.onmessage = (e) => {
    if (audioQueue.length < 20) {  // ~1.2s buffer max
      audioQueue.push(e.data);
    }
  };
  audioWs.onclose = () => { audioPlaying = false; updateAudioBtn(); };

  audioPlaying = true;
  updateAudioBtn();
}

function stopAudio() {
  if (audioWs) { audioWs.close(); audioWs = null; }
  if (audioNode) { audioNode.disconnect(); audioNode = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  audioQueue = [];
  audioPlaying = false;
  updateAudioBtn();
}

function setAudioVolume(val) {
  audioVolume = parseFloat(val);
  document.getElementById('vol-val').textContent = Math.round(val * 100) + '%';
}

function toggleMute() {
  audioMuted = !audioMuted;
  document.getElementById('mute-btn').textContent = audioMuted ? 'Unmute' : 'Mute';
}

function updateAudioBtn() {
  const btn = document.getElementById('audio-btn');
  if (btn) {
    btn.textContent = audioPlaying ? 'Stop Audio' : 'Listen';
    btn.classList.toggle('active', audioPlaying);
  }
}

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

  // Sort channels
  channels = [...channels];
  switch(sortMode) {
    case 'recent':
      channels.sort((a, b) => {
        const aTime = a.last_voice || a.last_signal || '';
        const bTime = b.last_voice || b.last_signal || '';
        return bTime.localeCompare(aTime);
      });
      break;
    case 'signal':
      channels.sort((a, b) => b.signal_count - a.signal_count);
      break;
    case 'voice':
      channels.sort((a, b) => b.voice_count - a.voice_count);
      break;
    case 'name':
      channels.sort((a, b) => a.name.localeCompare(b.name));
      break;
    case 'freq':
    default:
      channels.sort((a, b) => a.freq - b.freq);
      break;
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
      <div class="freq-col" onclick="tuneToChannel(${ch.freq})" style="cursor:pointer" title="Click to listen">${ch.freq_mhz}</div>
      <div class="name-col"><span class="ch-name" onclick="renameChannel(${ch.freq},'${ch.name.replace(/'/g,"\\'")}')">${ch.name}</span><br><span class="group">${ch.group}</span></div>
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
  if (state.current_detection === 'listening') {
    scan.textContent = 'LISTENING';
    scan.className = 'badge badge-listening';
  } else if (state.paused_on_voice) {
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

  // SDR source
  document.getElementById('sdr-source').textContent = state.sdr_source || '';

  // Scanning window
  const sw = document.getElementById('scan-window');
  if (state.current_window_lo_mhz && state.current_window_hi_mhz) {
    sw.textContent = state.current_window_lo_mhz + ' - ' + state.current_window_hi_mhz + ' MHz';
  } else {
    sw.textContent = '';
  }

  // Auto-discover button
  const discBtn = document.getElementById('btn-discover');
  if (discBtn) {
    discBtn.classList.toggle('active', !!state.auto_discover);
    discBtn.textContent = state.auto_discover ? 'Discover ON' : 'Auto-Discover';
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

async function renameChannel(freq, currentName) {
  const name = prompt('Rename channel:', currentName);
  if (name && name !== currentName) {
    await api('/channel/rename', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({freq, name})});
    poll();
  }
}

async function toggleAutoDiscover() {
  await api('/auto-discover', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({})});
  poll();
}

async function tuneToChannel(freq) {
  await api('/tune/' + freq, {method: 'POST'});
  // Auto-start audio when tuning
  if (!audioPlaying) toggleAudio();
  poll();
}

function sortChannels(mode) {
  sortMode = mode;
  renderChannels();
}

function renderBands() {
  if (!state.channels) return;
  const bands = {};
  state.channels.forEach(ch => {
    if (!bands[ch.group]) bands[ch.group] = {channels: [], signals: 0, voices: 0};
    bands[ch.group].channels.push(ch);
    bands[ch.group].signals += ch.signal_count;
    bands[ch.group].voices += ch.voice_count;
  });

  const el = document.getElementById('band-overview');
  el.innerHTML = Object.entries(bands).map(([name, b]) => {
    const freqs = b.channels.map(c => c.freq);
    const lo = (Math.min(...freqs) / 1e6).toFixed(3);
    const hi = (Math.max(...freqs) / 1e6).toFixed(3);
    const isActive = state.current_band === name;
    const sigText = b.signals > 0 ? b.signals + ' sig' : '';
    const voiceText = b.voices > 0 ? ', ' + b.voices + ' voice' : '';
    return `<div class="band-card ${isActive ? 'active' : ''}">
      <div class="band-name">${name}</div>
      <div class="band-range">${lo} - ${hi} MHz</div>
      <div class="band-signals">${sigText}${voiceText}</div>
    </div>`;
  }).join('');
}

function renderFFT() {
  const canvas = document.getElementById('fft-canvas');
  if (!canvas || !state.fft_data || state.fft_data.length === 0) return;

  const ctx = canvas.getContext('2d');
  canvas.width = canvas.clientWidth;
  canvas.height = 120;
  const data = state.fft_data;
  const w = canvas.width;
  const h = canvas.height;

  // Clear
  ctx.fillStyle = '#0a0a0a';
  ctx.fillRect(0, 0, w, h);

  // Draw grid lines
  ctx.strokeStyle = '#222';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {
    const y = (i / 4) * h;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  // Find data range for scaling
  const minDb = -80;
  const maxDb = Math.max(...data, -20);
  const squelch = state.squelch_level || -45;

  // Draw squelch line
  const squelchY = h - ((squelch - minDb) / (maxDb - minDb)) * h;
  ctx.strokeStyle = '#a00';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, squelchY);
  ctx.lineTo(w, squelchY);
  ctx.stroke();
  ctx.setLineDash([]);

  // Draw spectrum
  ctx.beginPath();
  ctx.strokeStyle = '#0f0';
  ctx.lineWidth = 1.5;
  for (let i = 0; i < data.length; i++) {
    const x = (i / data.length) * w;
    const db = Math.max(data[i], minDb);
    const y = h - ((db - minDb) / (maxDb - minDb)) * h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill below the line with gradient
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = 'rgba(0, 255, 0, 0.1)';
  ctx.fill();

  // Draw channel markers if we know the window
  if (state.current_window_center && state.current_window_bw && state.channels) {
    const loFreq = state.current_window_center - state.current_window_bw / 2;
    const hiFreq = state.current_window_center + state.current_window_bw / 2;

    ctx.font = '9px monospace';
    ctx.textAlign = 'center';

    state.channels.forEach(ch => {
      if (ch.freq >= loFreq && ch.freq <= hiFreq) {
        const x = ((ch.freq - loFreq) / (hiFreq - loFreq)) * w;

        // Marker line
        ctx.strokeStyle = ch.active ? '#0f0' : '#444';
        ctx.lineWidth = ch.active ? 2 : 0.5;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();

        // Label (only if active or has signal)
        if (ch.active || ch.signal_count > 0) {
          ctx.fillStyle = ch.active ? '#0f0' : '#888';
          ctx.fillText(ch.name, x, 10);
        }
      }
    });
  }

  // Window frequency labels
  if (state.current_window_lo_mhz && state.current_window_hi_mhz) {
    ctx.font = '10px monospace';
    ctx.fillStyle = '#666';
    ctx.textAlign = 'left';
    ctx.fillText(state.current_window_lo_mhz + ' MHz', 4, h - 4);
    ctx.textAlign = 'right';
    ctx.fillText(state.current_window_hi_mhz + ' MHz', w - 4, h - 4);
  }
}

async function poll() {
  try {
    state = await api('/state');
    lastPollOk = true;
    pollFailCount = 0;
    updateHeader();
    renderChannels();
    renderBands();
    renderFFT();
    renderActivity();
    if (!document.querySelector('.group-filter button')) renderGroups();
  } catch(e) {
    lastPollOk = false;
    pollFailCount++;
    console.error('Poll error:', e);
  }
  updateConnectionStatus();
}

function updateConnectionStatus() {
  const el = document.getElementById('conn-status');
  if (!el) return;
  if (lastPollOk) {
    el.textContent = 'Connected';
    el.style.color = '#0a0';
  } else {
    el.textContent = 'Disconnected (' + pollFailCount + ')';
    el.style.color = '#a00';
  }
}

function showSdrInfo() {
  if (!state.sdr_info) return;
  const info = state.sdr_info;
  const lines = Object.entries(info).map(([k,v]) => k + ': ' + v).join('\\n');
  alert('SDR Device Info\\n\\n' + lines);
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


@app.route("/api/channel/add", methods=["POST"])
def add_channel():
    data = request.get_json()
    ch = scanner.add_channel(
        freq=data["freq"],
        name=data.get("name", ""),
        group=data.get("group", "Discovered"),
        mod=data.get("mod", "nfm"),
        bandwidth=data.get("bandwidth", 12500),
    )
    return jsonify({"ok": True, "channel": ch.name})


@app.route("/api/channel/rename", methods=["POST"])
def rename_channel():
    data = request.get_json()
    ok = scanner.rename_channel(data["freq"], data["name"])
    return jsonify({"ok": ok})


@app.route("/api/auto-discover", methods=["POST"])
def toggle_auto_discover():
    data = request.get_json()
    scanner.state.auto_discover = data.get("enabled", not scanner.state.auto_discover)
    return jsonify({"ok": True, "auto_discover": scanner.state.auto_discover})


@app.route("/api/tune/<int:freq>", methods=["POST"])
def tune_to(freq: int):
    """Tune to a specific channel for continuous listening."""
    scanner.tune_to_channel(freq)
    return jsonify({"ok": True})


@app.route("/api/listen/stop", methods=["POST"])
def stop_listen():
    scanner.stop_listening()
    return jsonify({"ok": True})


@sock.route("/ws/audio")
def audio_ws(ws):
    """Stream PCM audio to browser via WebSocket."""
    if not broadcaster:
        ws.close()
        return

    # Send audio format header
    ws.send(struct.pack('<HHH', 16000, 16, 1))  # sample_rate, bit_depth, channels

    q = broadcaster.subscribe()
    try:
        while True:
            try:
                data = q.get(timeout=1.0)
                ws.send(data)
            except queue.Empty:
                # Send keepalive silence
                pass
    except Exception:
        pass
    finally:
        broadcaster.unsubscribe(q)
