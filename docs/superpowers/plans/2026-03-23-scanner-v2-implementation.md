# Scanner V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a signal-driven radio scanner as a server-side OpenWebRX+ extension with a mobile-first web UI, SQLite logging, and audio recording.

**Architecture:** Scanner service runs inside the OpenWebRX+ process, implementing `SdrSourceEventClient` to control the SDR. It creates its own `FftChain` for power spectrum analysis, retunes across the full frequency range via `setCenterFreq()`, and uses `DspManager`-style chains for demod/recording. A custom mobile web UI at `/scanner` connects via WebSocket for real-time state and audio.

**Tech Stack:** Python 3.5+ (OpenWebRX+ runtime), csdr/pycsdr (DSP), SQLite3, vanilla JS + Web Audio API, ffmpeg (Opus encoding)

**Codebase:** All server-side code is added to the OpenWebRX+ fork at `/home/michael/Projects/Radio/OpenWebRX/openwebrx+`. Create a `feat/scanner-v2` branch there. The pi-sdr repo tracks design docs and planning only.

**Spec:** `docs/superpowers/specs/2026-03-23-scanner-v2-design.md`
**Research:** `docs/superpowers/research/2026-03-23-openwebrx-internals.md`

---

## File Structure

All new files are in the OpenWebRX+ fork. Existing files are modified minimally.

```
openwebrx+/
├── owrx/
│   ├── scanner/
│   │   ├── __init__.py              # ScannerService class (SdrSourceEventClient)
│   │   ├── sweep.py                 # FrequencySweeper — manages scan windows, retune logic
│   │   ├── detector.py              # SignalDetector — FFT peak finding, noise floor tracking
│   │   ├── classifier.py            # ClassificationPipeline — mode ID, VAD, pluggable stages
│   │   ├── recorder.py              # ScannerRecorder — Opus recording with storage management
│   │   ├── db.py                    # ScannerDatabase — SQLite schema, queries
│   │   └── config.py                # Scanner config defaults + property integration
│   ├── controllers/
│   │   └── scanner.py               # NEW: REST API controller for scanner endpoints
│   ├── http.py                      # MODIFY: add scanner routes
│   ├── connection.py                # MODIFY: add scanner WebSocket message handling
│   └── config/
│       └── defaults.py              # MODIFY: add scanner config defaults
├── htdocs/
│   ├── scanner.html                 # NEW: mobile scanner UI entry point
│   ├── scanner/
│   │   ├── app.js                   # Scanner UI app — WebSocket, state management
│   │   ├── audio.js                 # Mobile audio engine (ADPCM decode, Web Audio)
│   │   ├── views.js                 # Activity feed, listening view, log, settings
│   │   └── scanner.css              # Mobile-first styles
│   └── lib/
│       └── Scanner.js               # REFERENCE ONLY: existing client-side scanner
├── test/
│   ├── scanner/
│   │   ├── test_detector.py         # Signal detection unit tests
│   │   ├── test_classifier.py       # Classification pipeline tests
│   │   ├── test_db.py               # Database schema + query tests
│   │   ├── test_sweep.py            # Sweep logic tests
│   │   └── test_recorder.py         # Recording tests
│   └── integration/
│       └── test_scanner_service.py  # Integration test with IQ FileSource
└── docker-compose.dev.yml           # NEW: dev environment for scanner work
```

---

### Task 1: Dev environment — Docker Compose + branch setup

**Files:**
- Create: `docker-compose.dev.yml` in OpenWebRX+ fork
- Create: `owrx/scanner/__init__.py` (empty package)

This task sets up the development loop: edit Python → rebuild → test.

- [ ] **Step 1: Create feature branch in OpenWebRX+ fork**

```bash
cd /home/michael/Projects/Radio/OpenWebRX/openwebrx+
git checkout main
git pull
git checkout -b feat/scanner-v2
```

- [ ] **Step 2: Create `docker-compose.dev.yml`**

```yaml
services:
  openwebrx:
    build:
      context: .
      dockerfile: Dockerfile.dev
    ports:
      - "8073:8073"
    devices:
      - /dev/bus/usb:/dev/bus/usb
    volumes:
      - ./owrx:/opt/openwebrx/owrx
      - ./htdocs:/opt/openwebrx/htdocs
      - ./csdr:/opt/openwebrx/csdr
      - owrx-config:/etc/openwebrx
      - owrx-data:/var/lib/openwebrx

volumes:
  owrx-config:
  owrx-data:
```

Note: Volume-mounting `owrx/`, `htdocs/`, and `csdr/` over the installed copy means code changes are reflected without rebuilding the Docker image. Only need to restart the container.

- [ ] **Step 3: Create scanner package**

```bash
mkdir -p owrx/scanner
touch owrx/scanner/__init__.py
```

- [ ] **Step 4: Verify dev environment starts**

```bash
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml logs -f
# Expected: OpenWebRX+ starts on port 8073, SDR device detected
```

Open `http://localhost:8073` to verify the standard UI works.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.dev.yml owrx/scanner/__init__.py
git commit -m "chore: add dev Docker Compose and scanner package skeleton"
```

---

### Task 2: SQLite database schema + data layer

**Files:**
- Create: `owrx/scanner/db.py`
- Create: `test/scanner/test_db.py`

The database is the foundation — everything else writes to it.

- [ ] **Step 1: Create test directory**

```bash
mkdir -p test/scanner
touch test/scanner/__init__.py
```

- [ ] **Step 2: Write failing tests for database**

Create `test/scanner/test_db.py`:

```python
import unittest
import tempfile
import os
from datetime import datetime, timedelta


class TestScannerDatabase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        # Import here so test fails clearly if module doesn't exist
        from owrx.scanner.db import ScannerDatabase
        self.db = ScannerDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_creates_tables_on_init(self):
        tables = self.db.list_tables()
        self.assertIn("detections", tables)
        self.assertIn("bookmarks", tables)
        self.assertIn("scan_sessions", tables)

    def test_log_detection(self):
        det_id = self.db.log_detection(
            frequency_hz=162475000,
            mode="nfm",
            peak_power_db=-45.2,
            snr_db=12.5,
            classification="voice",
            filter_result="passed",
            bookmark_label="NOAA WX4",
        )
        self.assertIsInstance(det_id, int)
        det = self.db.get_detection(det_id)
        self.assertEqual(det["frequency_hz"], 162475000)
        self.assertEqual(det["mode"], "nfm")
        self.assertIsNone(det["duration_sec"])

    def test_update_detection_duration(self):
        det_id = self.db.log_detection(
            frequency_hz=146520000, mode="nfm",
            peak_power_db=-50.0, snr_db=8.0,
        )
        self.db.update_detection(det_id, duration_sec=4.5,
                                  recording_path="/tmp/rec.ogg")
        det = self.db.get_detection(det_id)
        self.assertEqual(det["duration_sec"], 4.5)
        self.assertEqual(det["recording_path"], "/tmp/rec.ogg")

    def test_get_recent_detections(self):
        for freq in [162475000, 146520000, 121500000]:
            self.db.log_detection(
                frequency_hz=freq, mode="nfm",
                peak_power_db=-50.0, snr_db=8.0,
                filter_result="passed",
            )
        recent = self.db.get_recent_detections(limit=2)
        self.assertEqual(len(recent), 2)

    def test_get_most_active_frequencies(self):
        # Log multiple detections on same frequency
        for _ in range(5):
            self.db.log_detection(
                frequency_hz=162475000, mode="nfm",
                peak_power_db=-45.0, snr_db=12.0,
            )
        for _ in range(2):
            self.db.log_detection(
                frequency_hz=146520000, mode="nfm",
                peak_power_db=-50.0, snr_db=8.0,
            )
        active = self.db.get_most_active(hours=24, limit=10)
        self.assertEqual(active[0]["frequency_hz"], 162475000)
        self.assertEqual(active[0]["count"], 5)

    def test_add_bookmark(self):
        bm_id = self.db.add_bookmark(
            frequency_hz=162475000,
            label="NOAA WX4",
            mode="nfm",
        )
        bm = self.db.get_bookmark(bm_id)
        self.assertEqual(bm["label"], "NOAA WX4")

    def test_start_and_stop_scan_session(self):
        session_id = self.db.start_session(config={"freq_start": 25000000})
        self.db.stop_session(session_id)
        session = self.db.get_session(session_id)
        self.assertIsNotNone(session["stopped_at"])

    def test_clear_recording_path(self):
        det_id = self.db.log_detection(
            frequency_hz=162475000, mode="nfm",
            peak_power_db=-45.0, snr_db=12.0,
            recording_path="/tmp/rec.ogg",
        )
        self.db.clear_recording_path(det_id)
        det = self.db.get_detection(det_id)
        self.assertIsNone(det["recording_path"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python3 -m pytest test/scanner/test_db.py -v
```

Expected: `ModuleNotFoundError: No module named 'owrx.scanner.db'`

- [ ] **Step 4: Implement `owrx/scanner/db.py`**

```python
import sqlite3
import json
from datetime import datetime


class ScannerDatabase:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        c = self.conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                frequency_hz INTEGER NOT NULL,
                bandwidth_hz INTEGER,
                mode TEXT,
                peak_power_db REAL,
                snr_db REAL,
                duration_sec REAL,
                classification TEXT,
                filter_result TEXT,
                bookmark_label TEXT,
                recording_path TEXT,
                transcription TEXT,
                summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_detections_time
                ON detections(timestamp);
            CREATE INDEX IF NOT EXISTS idx_detections_freq
                ON detections(frequency_hz);
            CREATE INDEX IF NOT EXISTS idx_detections_class
                ON detections(classification);

            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY,
                frequency_hz INTEGER NOT NULL,
                label TEXT NOT NULL,
                mode TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                last_heard TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_sessions (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                stopped_at TEXT,
                config_json TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def list_tables(self):
        c = self.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in c.fetchall()]

    def log_detection(self, frequency_hz, mode=None, peak_power_db=None,
                      snr_db=None, bandwidth_hz=None, classification=None,
                      filter_result=None, bookmark_label=None,
                      recording_path=None):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO detections
               (timestamp, frequency_hz, bandwidth_hz, mode, peak_power_db,
                snr_db, classification, filter_result, bookmark_label,
                recording_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), frequency_hz, bandwidth_hz,
             mode, peak_power_db, snr_db, classification, filter_result,
             bookmark_label, recording_path),
        )
        self.conn.commit()
        return c.lastrowid

    def get_detection(self, det_id):
        c = self.conn.cursor()
        c.execute("SELECT * FROM detections WHERE id = ?", (det_id,))
        return dict(c.fetchone())

    def update_detection(self, det_id, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [det_id]
        self.conn.execute(
            f"UPDATE detections SET {sets} WHERE id = ?", vals
        )
        self.conn.commit()

    def clear_recording_path(self, det_id):
        self.update_detection(det_id, recording_path=None)

    def get_recent_detections(self, limit=20, filter_result=None):
        query = "SELECT * FROM detections"
        params = []
        if filter_result:
            query += " WHERE filter_result = ?"
            params.append(filter_result)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        c = self.conn.cursor()
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def get_most_active(self, hours=24, limit=10):
        c = self.conn.cursor()
        c.execute(
            """SELECT frequency_hz, mode, COUNT(*) as count,
                      MAX(peak_power_db) as max_power
               FROM detections
               WHERE timestamp > datetime('now', ?)
               GROUP BY frequency_hz
               ORDER BY count DESC
               LIMIT ?""",
            (f"-{hours} hours", limit),
        )
        return [dict(row) for row in c.fetchall()]

    def add_bookmark(self, frequency_hz, label, mode=None, notes=None):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO bookmarks
               (frequency_hz, label, mode, notes, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (frequency_hz, label, mode, notes,
             datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return c.lastrowid

    def get_bookmark(self, bm_id):
        c = self.conn.cursor()
        c.execute("SELECT * FROM bookmarks WHERE id = ?", (bm_id,))
        return dict(c.fetchone())

    def get_all_bookmarks(self):
        c = self.conn.cursor()
        c.execute("SELECT * FROM bookmarks ORDER BY frequency_hz")
        return [dict(row) for row in c.fetchall()]

    def start_session(self, config):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO scan_sessions (started_at, config_json)
               VALUES (?, ?)""",
            (datetime.utcnow().isoformat(), json.dumps(config)),
        )
        self.conn.commit()
        return c.lastrowid

    def stop_session(self, session_id):
        self.conn.execute(
            "UPDATE scan_sessions SET stopped_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), session_id),
        )
        self.conn.commit()

    def get_session(self, session_id):
        c = self.conn.cursor()
        c.execute("SELECT * FROM scan_sessions WHERE id = ?", (session_id,))
        return dict(c.fetchone())

    def close(self):
        self.conn.close()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest test/scanner/test_db.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add owrx/scanner/db.py test/scanner/
git commit -m "feat(scanner): add SQLite database schema and data layer"
```

---

### Task 3: Signal detector — FFT peak finding + noise floor tracking

**Files:**
- Create: `owrx/scanner/detector.py`
- Create: `test/scanner/test_detector.py`

The signal detector processes FFT power spectrum arrays and finds signals above the noise floor. It operates on numpy arrays — no SDR dependency, fully testable with synthetic data.

- [ ] **Step 1: Write failing tests**

Create `test/scanner/test_detector.py`:

```python
import unittest
import numpy as np


class TestSignalDetector(unittest.TestCase):
    def setUp(self):
        from owrx.scanner.detector import SignalDetector
        self.detector = SignalDetector(
            fft_size=1024,
            sample_rate=2400000,
            snr_threshold_db=10.0,
        )

    def test_no_signals_in_noise(self):
        """Flat noise floor should produce no detections."""
        rng = np.random.default_rng(42)
        fft_power = rng.normal(-90.0, 1.0, 1024).astype(np.float32)
        signals = self.detector.detect(fft_power, center_freq=100000000)
        self.assertEqual(len(signals), 0)

    def test_single_strong_signal(self):
        """One peak 20 dB above noise should be detected."""
        fft_power = np.full(1024, -90.0, dtype=np.float32)
        # Inject signal at bin 512 (center), ~5 bins wide
        fft_power[510:515] = -70.0
        signals = self.detector.detect(fft_power, center_freq=100000000)
        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # Frequency should be near center
        self.assertAlmostEqual(sig["frequency_hz"], 100000000, delta=50000)
        self.assertGreater(sig["snr_db"], 10.0)

    def test_multiple_signals(self):
        """Two peaks should produce two detections."""
        fft_power = np.full(1024, -90.0, dtype=np.float32)
        fft_power[200:205] = -65.0  # Signal 1
        fft_power[800:805] = -70.0  # Signal 2
        signals = self.detector.detect(fft_power, center_freq=100000000)
        self.assertEqual(len(signals), 2)

    def test_weak_signal_below_threshold(self):
        """Signal only 5 dB above noise (below 10 dB threshold) is ignored."""
        fft_power = np.full(1024, -90.0, dtype=np.float32)
        fft_power[512:515] = -85.0  # Only 5 dB above noise
        signals = self.detector.detect(fft_power, center_freq=100000000)
        self.assertEqual(len(signals), 0)

    def test_noise_floor_tracking(self):
        """Noise floor should update over successive calls."""
        fft_power = np.full(1024, -80.0, dtype=np.float32)
        self.detector.detect(fft_power, center_freq=100000000)
        self.assertAlmostEqual(self.detector.noise_floor_db, -80.0, delta=2.0)

    def test_signal_bandwidth_estimation(self):
        """Wide signal should report larger bandwidth than narrow one."""
        fft_power = np.full(1024, -90.0, dtype=np.float32)
        fft_power[500:524] = -70.0  # ~24 bins wide
        signals = self.detector.detect(fft_power, center_freq=100000000)
        self.assertEqual(len(signals), 1)
        # 24 bins * (2.4 MHz / 1024) ≈ 56 kHz
        self.assertGreater(signals[0]["bandwidth_hz"], 40000)

    def test_bin_to_frequency_conversion(self):
        """Bin at edge of FFT should map to correct frequency offset."""
        freq = self.detector._bin_to_freq(0, center_freq=100000000)
        # Bin 0 = center_freq - sample_rate/2
        self.assertEqual(freq, 100000000 - 1200000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest test/scanner/test_detector.py -v
```

Expected: `ModuleNotFoundError: No module named 'owrx.scanner.detector'`

- [ ] **Step 3: Implement `owrx/scanner/detector.py`**

```python
import numpy as np
from typing import NamedTuple


class DetectedSignal(dict):
    """A signal found in the FFT power spectrum."""
    pass


class SignalDetector:
    """Finds signals in FFT power spectrum data.

    Operates on dB-scale power arrays. Tracks noise floor with IIR filter.
    Returns list of detected signals with frequency, power, SNR, bandwidth.
    """

    def __init__(self, fft_size, sample_rate, snr_threshold_db=10.0,
                 noise_alpha=0.1, min_signal_bins=2):
        self.fft_size = fft_size
        self.sample_rate = sample_rate
        self.snr_threshold_db = snr_threshold_db
        self.noise_alpha = noise_alpha
        self.min_signal_bins = min_signal_bins
        self.noise_floor_db = None
        self._bin_width_hz = sample_rate / fft_size

    def detect(self, fft_power_db, center_freq):
        """Find signals in FFT power spectrum.

        Args:
            fft_power_db: numpy array of power values in dB, length=fft_size
            center_freq: center frequency of this FFT window in Hz

        Returns:
            List of DetectedSignal dicts with keys:
            frequency_hz, bandwidth_hz, peak_power_db, snr_db
        """
        # Update noise floor estimate (median of all bins)
        current_noise = float(np.median(fft_power_db))
        if self.noise_floor_db is None:
            self.noise_floor_db = current_noise
        else:
            self.noise_floor_db += self.noise_alpha * (
                current_noise - self.noise_floor_db
            )

        threshold = self.noise_floor_db + self.snr_threshold_db

        # Find bins above threshold
        above = fft_power_db > threshold

        # Group contiguous above-threshold bins into signals
        signals = []
        in_signal = False
        start_bin = 0

        for i in range(len(above)):
            if above[i] and not in_signal:
                start_bin = i
                in_signal = True
            elif not above[i] and in_signal:
                if i - start_bin >= self.min_signal_bins:
                    signals.append(self._make_signal(
                        fft_power_db, start_bin, i, center_freq
                    ))
                in_signal = False

        # Handle signal at end of array
        if in_signal and len(above) - start_bin >= self.min_signal_bins:
            signals.append(self._make_signal(
                fft_power_db, start_bin, len(above), center_freq
            ))

        return signals

    def _make_signal(self, fft_power_db, start_bin, end_bin, center_freq):
        """Create a DetectedSignal from a range of FFT bins."""
        segment = fft_power_db[start_bin:end_bin]
        peak_bin = start_bin + int(np.argmax(segment))
        peak_power = float(segment.max())

        return DetectedSignal(
            frequency_hz=self._bin_to_freq(peak_bin, center_freq),
            bandwidth_hz=int((end_bin - start_bin) * self._bin_width_hz),
            peak_power_db=peak_power,
            snr_db=peak_power - self.noise_floor_db,
        )

    def _bin_to_freq(self, bin_idx, center_freq):
        """Convert FFT bin index to frequency in Hz.

        FFT bins are ordered: DC at center, negative freqs left, positive right.
        Bin 0 = center_freq - sample_rate/2
        Bin N/2 = center_freq
        Bin N-1 = center_freq + sample_rate/2
        """
        offset = (bin_idx - self.fft_size / 2) * self._bin_width_hz
        return int(center_freq + offset)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest test/scanner/test_detector.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add owrx/scanner/detector.py test/scanner/test_detector.py
git commit -m "feat(scanner): add signal detector with FFT peak finding and noise floor tracking"
```

---

### Task 4: Frequency sweeper — scan window management + retune logic

**Files:**
- Create: `owrx/scanner/sweep.py`
- Create: `test/scanner/test_sweep.py`

Manages which frequency window to tune to next. Pure logic — no SDR interaction. Testable without hardware.

- [ ] **Step 1: Write failing tests**

Create `test/scanner/test_sweep.py`:

```python
import unittest


class TestFrequencySweeper(unittest.TestCase):
    def setUp(self):
        from owrx.scanner.sweep import FrequencySweeper
        self.sweeper = FrequencySweeper(
            freq_start=100000000,   # 100 MHz
            freq_stop=110000000,    # 110 MHz
            sample_rate=2400000,    # 2.4 MHz window
            usable_bw_ratio=0.8,
        )

    def test_initial_window(self):
        window = self.sweeper.current_window()
        self.assertEqual(window["center_freq"], 100000000 + 1200000)

    def test_advance_produces_next_window(self):
        w1 = self.sweeper.current_window()
        self.sweeper.advance()
        w2 = self.sweeper.current_window()
        self.assertGreater(w2["center_freq"], w1["center_freq"])

    def test_wraps_around_at_end(self):
        # Advance past the end
        for _ in range(100):
            self.sweeper.advance()
        window = self.sweeper.current_window()
        # Should have wrapped back to start
        self.assertLessEqual(window["center_freq"], 112000000)

    def test_total_windows(self):
        count = self.sweeper.total_windows
        # 10 MHz range / (2.4 MHz * 0.8 usable) = ~5.2 -> 6 windows
        self.assertGreaterEqual(count, 5)
        self.assertLessEqual(count, 7)

    def test_skip_list(self):
        from owrx.scanner.sweep import FrequencySweeper
        sweeper = FrequencySweeper(
            freq_start=100000000,
            freq_stop=110000000,
            sample_rate=2400000,
            skip_ranges=[(103000000, 105000000)],
        )
        # All windows should avoid the skip range center
        for _ in range(sweeper.total_windows):
            w = sweeper.current_window()
            low = w["center_freq"] - 1200000
            high = w["center_freq"] + 1200000
            # Window shouldn't be entirely inside skip range
            if low >= 103000000 and high <= 105000000:
                self.fail(f"Window at {w['center_freq']} is inside skip range")
            sweeper.advance()

    def test_progress_fraction(self):
        self.assertAlmostEqual(self.sweeper.progress, 0.0, places=1)
        total = self.sweeper.total_windows
        for _ in range(total // 2):
            self.sweeper.advance()
        self.assertAlmostEqual(self.sweeper.progress, 0.5, delta=0.2)

    def test_demod_mode_for_frequency(self):
        from owrx.scanner.sweep import demod_mode_for_freq
        self.assertEqual(demod_mode_for_freq(91500000), "wfm")   # FM broadcast
        self.assertEqual(demod_mode_for_freq(121500000), "am")    # Air band
        self.assertEqual(demod_mode_for_freq(162475000), "nfm")   # NOAA/VHF
        self.assertEqual(demod_mode_for_freq(446000000), "nfm")   # GMRS


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest test/scanner/test_sweep.py -v
```

- [ ] **Step 3: Implement `owrx/scanner/sweep.py`**

```python
import math


# Frequency range → default demod mode
BAND_MODES = [
    (25000000, 30000000, "am"),      # HF CB, shortwave
    (30000000, 88000000, "nfm"),     # VHF low band
    (88000000, 108000000, "wfm"),    # FM broadcast
    (108000000, 137000000, "am"),    # Air band
    (137000000, 174000000, "nfm"),   # VHF high band
    (174000000, 400000000, "nfm"),   # UHF
    (400000000, 470000000, "nfm"),   # UHF public safety, GMRS
    (470000000, 960000000, "nfm"),   # UHF misc
    (960000000, 1700000000, "am"),   # L-band
]


def demod_mode_for_freq(freq_hz):
    """Return default demodulation mode for a frequency."""
    for low, high, mode in BAND_MODES:
        if low <= freq_hz < high:
            return mode
    return "nfm"


class FrequencySweeper:
    """Manages sequential sweep across a frequency range.

    Divides the range into windows sized by sample_rate * usable_bw_ratio,
    steps through them sequentially, and wraps around at the end.
    """

    def __init__(self, freq_start, freq_stop, sample_rate,
                 usable_bw_ratio=0.8, skip_ranges=None):
        self.freq_start = freq_start
        self.freq_stop = freq_stop
        self.sample_rate = sample_rate
        self.usable_bw = int(sample_rate * usable_bw_ratio)
        self.skip_ranges = skip_ranges or []

        # Build window list
        self._windows = []
        center = freq_start + sample_rate // 2
        while center - sample_rate // 2 < freq_stop:
            if not self._is_skipped(center):
                self._windows.append(center)
            center += self.usable_bw

        self._index = 0

    def _is_skipped(self, center_freq):
        """Check if a window is entirely inside a skip range."""
        low = center_freq - self.sample_rate // 2
        high = center_freq + self.sample_rate // 2
        for skip_low, skip_high in self.skip_ranges:
            if low >= skip_low and high <= skip_high:
                return True
        return False

    @property
    def total_windows(self):
        return len(self._windows)

    @property
    def progress(self):
        if not self._windows:
            return 0.0
        return self._index / len(self._windows)

    def current_window(self):
        """Return current scan window info."""
        center = self._windows[self._index]
        return {
            "center_freq": center,
            "sample_rate": self.sample_rate,
            "low_freq": center - self.sample_rate // 2,
            "high_freq": center + self.sample_rate // 2,
            "demod_mode": demod_mode_for_freq(center),
        }

    def advance(self):
        """Move to the next scan window. Wraps around at end."""
        self._index = (self._index + 1) % len(self._windows)

    def reset(self):
        """Reset to the beginning of the sweep."""
        self._index = 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest test/scanner/test_sweep.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add owrx/scanner/sweep.py test/scanner/test_sweep.py
git commit -m "feat(scanner): add frequency sweeper with band mode detection and skip lists"
```

---

### Task 5: Classification pipeline — pluggable stages with mode auto-detect

**Files:**
- Create: `owrx/scanner/classifier.py`
- Create: `test/scanner/test_classifier.py`

Pluggable pipeline: each stage decides pass/reject/log-only. First implementation: auto-detect demod mode by frequency. VAD is a placeholder that always passes (real VAD comes later).

- [ ] **Step 1: Write failing tests**

Create `test/scanner/test_classifier.py`:

```python
import unittest
import numpy as np


class TestClassificationPipeline(unittest.TestCase):
    def setUp(self):
        from owrx.scanner.classifier import ClassificationPipeline, AnyFilter
        self.pipeline = ClassificationPipeline(content_filter=AnyFilter())

    def test_classify_returns_result(self):
        result = self.pipeline.classify(
            frequency_hz=162475000,
            bandwidth_hz=12500,
            peak_power_db=-45.0,
            snr_db=15.0,
        )
        self.assertIn("mode", result)
        self.assertIn("classification", result)
        self.assertIn("action", result)

    def test_auto_mode_detection(self):
        result = self.pipeline.classify(
            frequency_hz=91500000,  # FM broadcast
            bandwidth_hz=200000,
            peak_power_db=-40.0, snr_db=20.0,
        )
        self.assertEqual(result["mode"], "wfm")

    def test_any_filter_passes_everything(self):
        result = self.pipeline.classify(
            frequency_hz=162475000,
            bandwidth_hz=12500,
            peak_power_db=-45.0, snr_db=15.0,
        )
        self.assertEqual(result["action"], "unsquelch")

    def test_voice_filter_placeholder(self):
        from owrx.scanner.classifier import ClassificationPipeline, VoiceFilter
        pipeline = ClassificationPipeline(content_filter=VoiceFilter())
        result = pipeline.classify(
            frequency_hz=162475000,
            bandwidth_hz=12500,
            peak_power_db=-45.0, snr_db=15.0,
        )
        # VoiceFilter placeholder passes everything (real VAD comes later)
        self.assertEqual(result["action"], "unsquelch")

    def test_custom_filter(self):
        from owrx.scanner.classifier import (
            ClassificationPipeline, ContentFilter,
        )

        class RejectAllFilter(ContentFilter):
            def check(self, signal_info):
                return "rejected"

        pipeline = ClassificationPipeline(content_filter=RejectAllFilter())
        result = pipeline.classify(
            frequency_hz=162475000,
            bandwidth_hz=12500,
            peak_power_db=-45.0, snr_db=15.0,
        )
        self.assertEqual(result["action"], "log-only")

    def test_bandwidth_suggests_wfm(self):
        """Wide bandwidth signal on non-broadcast freq should suggest WFM."""
        from owrx.scanner.classifier import estimate_mode_from_bandwidth
        mode = estimate_mode_from_bandwidth(180000)
        self.assertEqual(mode, "wfm")

    def test_bandwidth_suggests_nfm(self):
        from owrx.scanner.classifier import estimate_mode_from_bandwidth
        mode = estimate_mode_from_bandwidth(12500)
        self.assertEqual(mode, "nfm")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest test/scanner/test_classifier.py -v
```

- [ ] **Step 3: Implement `owrx/scanner/classifier.py`**

```python
from owrx.scanner.sweep import demod_mode_for_freq


def estimate_mode_from_bandwidth(bandwidth_hz):
    """Guess modulation mode from observed signal bandwidth."""
    if bandwidth_hz > 100000:
        return "wfm"
    elif bandwidth_hz > 20000:
        return "nfm"  # could be wide NFM
    elif bandwidth_hz > 5000:
        return "nfm"
    else:
        return "am"  # narrow signal, could be AM/CW/SSB


class ContentFilter:
    """Base class for content filters. Subclass and override check()."""

    def check(self, signal_info):
        """Check signal content.

        Args:
            signal_info: dict with frequency_hz, mode, bandwidth_hz, etc.

        Returns:
            "passed" — signal matches filter, should unsquelch
            "rejected" — signal doesn't match, skip
            "log-only" — log but don't unsquelch
        """
        raise NotImplementedError


class AnyFilter(ContentFilter):
    """Passes everything."""
    def check(self, signal_info):
        return "passed"


class VoiceFilter(ContentFilter):
    """Voice activity detection filter.

    Placeholder: passes everything. Real VAD (autocorrelation-based or
    Silero) will be added later.
    """
    def check(self, signal_info):
        # TODO: implement real VAD
        # For now, pass everything — equivalent to AnyFilter
        return "passed"


class ClassificationPipeline:
    """Classifies detected signals through a pluggable pipeline.

    Stages:
    1. Mode auto-detect (by frequency band and bandwidth)
    2. Content filter (voice, CW, any, custom)
    3. Action determination (unsquelch, notify, log-only)
    """

    def __init__(self, content_filter=None):
        self.content_filter = content_filter or AnyFilter()

    def classify(self, frequency_hz, bandwidth_hz, peak_power_db, snr_db,
                 audio_buffer=None):
        """Run classification pipeline on a detected signal.

        Returns dict with: mode, classification, filter_result, action
        """
        # Stage 1: Mode auto-detect
        mode = demod_mode_for_freq(frequency_hz)
        # Refine with bandwidth if available
        if bandwidth_hz:
            bw_mode = estimate_mode_from_bandwidth(bandwidth_hz)
            # Bandwidth-based mode overrides frequency-based for broadcast
            if bw_mode == "wfm" and mode != "am":
                mode = "wfm"

        # Stage 2: Basic classification
        classification = "unknown"
        if mode in ("nfm", "am", "wfm"):
            classification = "analog"

        # Stage 3: Content filter
        signal_info = {
            "frequency_hz": frequency_hz,
            "bandwidth_hz": bandwidth_hz,
            "mode": mode,
            "peak_power_db": peak_power_db,
            "snr_db": snr_db,
            "classification": classification,
        }
        filter_result = self.content_filter.check(signal_info)

        # Stage 4: Action
        if filter_result == "passed":
            action = "unsquelch"
        elif filter_result == "log-only":
            action = "log-only"
        else:
            action = "log-only"

        return {
            "mode": mode,
            "classification": classification,
            "filter_result": filter_result,
            "action": action,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest test/scanner/test_classifier.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add owrx/scanner/classifier.py test/scanner/test_classifier.py
git commit -m "feat(scanner): add classification pipeline with pluggable content filters"
```

---

### Task 6: Scanner config — integration with OpenWebRX+ property system

**Files:**
- Create: `owrx/scanner/config.py`
- Modify: `owrx/config/defaults.py`

Register scanner config defaults so they appear in OpenWebRX+'s property system and can be changed at runtime.

- [ ] **Step 1: Create `owrx/scanner/config.py`**

```python
# Scanner configuration keys and defaults.
# These integrate with OpenWebRX+'s PropertyLayer config system.

SCANNER_DEFAULTS = {
    "scanner_enabled": False,
    "scanner_freq_start": 25000000,       # 25 MHz
    "scanner_freq_stop": 1700000000,      # 1700 MHz
    "scanner_dwell_time_ms": 300,
    "scanner_squelch_threshold": -1,      # -1 = auto
    "scanner_hang_time_ms": 2000,
    "scanner_skip_list": [],              # list of freq_hz to skip
    "scanner_demod_mode": "auto",         # auto / nfm / am / wfm
    "scanner_content_filter": "any",      # any / voice
    "scanner_record_mode": "filter_matches",  # off / filter_matches / all
    "scanner_storage_path": "/var/lib/openwebrx/recordings",
    "scanner_max_storage_mb": 10240,
    "scanner_max_clip_sec": 300,
    "scanner_retention_days": 30,
    "scanner_recording_kbps": 24,
}
```

- [ ] **Step 2: Add scanner defaults to OpenWebRX+ config**

Modify `owrx/config/defaults.py` — append scanner defaults to the `defaultConfig` PropertyLayer. Find the end of the existing defaults (around line 436) and add:

```python
    # Scanner
    scanner_enabled=False,
    scanner_freq_start=25000000,
    scanner_freq_stop=1700000000,
    scanner_dwell_time_ms=300,
    scanner_squelch_threshold=-1,
    scanner_hang_time_ms=2000,
    scanner_skip_list=[],
    scanner_demod_mode="auto",
    scanner_content_filter="any",
    scanner_record_mode="filter_matches",
    scanner_storage_path="/var/lib/openwebrx/recordings",
    scanner_max_storage_mb=10240,
    scanner_max_clip_sec=300,
    scanner_retention_days=30,
    scanner_recording_kbps=24,
```

- [ ] **Step 3: Verify OpenWebRX+ still starts with new defaults**

```bash
docker compose -f docker-compose.dev.yml restart
docker compose -f docker-compose.dev.yml logs --tail=20
```

Expected: No errors related to scanner config.

- [ ] **Step 4: Commit**

```bash
git add owrx/scanner/config.py owrx/config/defaults.py
git commit -m "feat(scanner): add scanner config defaults to property system"
```

---

### Task 7: Scanner service — main scan loop integrating all components

**Files:**
- Create: `owrx/scanner/__init__.py` (replace empty file with ScannerService)
- Create: `test/scanner/test_scanner_service.py`

This is the core service that ties everything together. It implements `SdrSourceEventClient`, creates the scan loop, and coordinates sweep → detect → classify → log.

Note: This task cannot be fully integration-tested without OpenWebRX+ running. Unit tests mock the SdrSource interface.

- [ ] **Step 1: Write `owrx/scanner/__init__.py`**

```python
import threading
import logging
import time

from owrx.scanner.sweep import FrequencySweeper
from owrx.scanner.detector import SignalDetector
from owrx.scanner.classifier import (
    ClassificationPipeline, AnyFilter, VoiceFilter,
)
from owrx.scanner.db import ScannerDatabase
from owrx.config import Config

logger = logging.getLogger(__name__)


class ScannerState:
    """Observable scanner state for WebSocket clients."""

    IDLE = "idle"
    SCANNING = "scanning"
    LISTENING = "listening"
    PAUSED = "paused"

    def __init__(self):
        self.status = self.IDLE
        self.current_freq = 0
        self.current_mode = ""
        self.current_label = ""
        self.signal_strength = 0.0
        self.scan_progress = 0.0
        self.active_signals = []
        self._listeners = []

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        self._listeners.remove(callback)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb(self.to_dict())
            except Exception:
                logger.exception("Error notifying scanner state listener")

    def to_dict(self):
        return {
            "status": self.status,
            "current_freq": self.current_freq,
            "current_mode": self.current_mode,
            "current_label": self.current_label,
            "signal_strength": self.signal_strength,
            "scan_progress": self.scan_progress,
            "active_signals": self.active_signals,
        }

    def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self._notify()


class ScannerService:
    """Server-side scanner service for OpenWebRX+.

    Implements the scan cycle:
    1. Retune SDR to next window
    2. Read FFT power spectrum
    3. Detect signals above noise floor
    4. Classify and filter
    5. Log to database, optionally record
    6. If signal passes filter: pause scan, stream audio
    7. When signal drops: resume scanning
    """

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.state = ScannerState()
        self.db = None
        self._sdr_source = None
        self._thread = None
        self._stop_event = threading.Event()
        self._hold_freq = None

    def start(self, sdr_source):
        """Start scanning with the given SDR source."""
        if self._thread and self._thread.is_alive():
            logger.warning("Scanner already running")
            return

        pm = Config.get()
        self._sdr_source = sdr_source
        sample_rate = sdr_source.getProps()["samp_rate"]

        # Init components
        self.sweeper = FrequencySweeper(
            freq_start=pm["scanner_freq_start"],
            freq_stop=pm["scanner_freq_stop"],
            sample_rate=sample_rate,
        )
        self.detector = SignalDetector(
            fft_size=pm["fft_size"],
            sample_rate=sample_rate,
            snr_threshold_db=(
                pm["scanner_squelch_threshold"]
                if pm["scanner_squelch_threshold"] > 0 else 10.0
            ),
        )

        filter_name = pm["scanner_content_filter"]
        if filter_name == "voice":
            content_filter = VoiceFilter()
        else:
            content_filter = AnyFilter()
        self.classifier = ClassificationPipeline(
            content_filter=content_filter,
        )

        db_path = pm.get(
            "scanner_db_path",
            "/var/lib/openwebrx/scanner.db",
        )
        self.db = ScannerDatabase(db_path)
        self._session_id = self.db.start_session(config={
            "freq_start": pm["scanner_freq_start"],
            "freq_stop": pm["scanner_freq_stop"],
            "sample_rate": sample_rate,
        })

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="scanner",
        )
        self._thread.start()
        self.state.update(status=ScannerState.SCANNING)
        logger.info("Scanner started: %d-%d MHz",
                     pm["scanner_freq_start"] // 1000000,
                     pm["scanner_freq_stop"] // 1000000)

    def stop(self):
        """Stop scanning."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.db and hasattr(self, '_session_id'):
            self.db.stop_session(self._session_id)
        self.state.update(status=ScannerState.IDLE)
        logger.info("Scanner stopped")

    def pause(self):
        self.state.update(status=ScannerState.PAUSED)

    def resume(self):
        self.state.update(status=ScannerState.SCANNING)

    def skip(self):
        """Skip current signal, advance to next."""
        self._hold_freq = None
        self.state.update(status=ScannerState.SCANNING)

    def hold(self, freq_hz=None):
        """Hold on current or specified frequency."""
        self._hold_freq = freq_hz or self.state.current_freq
        self.state.update(status=ScannerState.LISTENING)

    def _scan_loop(self):
        """Main scan loop — runs in background thread."""
        pm = Config.get()
        dwell_time = pm["scanner_dwell_time_ms"] / 1000.0

        while not self._stop_event.is_set():
            if self.state.status == ScannerState.PAUSED:
                time.sleep(0.1)
                continue

            if self._hold_freq:
                time.sleep(0.1)
                continue

            # Get next window
            window = self.sweeper.current_window()
            self.state.update(
                current_freq=window["center_freq"],
                scan_progress=self.sweeper.progress,
            )

            # Retune SDR
            try:
                self._sdr_source.setCenterFreq(window["center_freq"])
            except Exception:
                logger.exception("Failed to retune SDR")
                time.sleep(1)
                continue

            # Dwell
            time.sleep(dwell_time)

            # TODO: Read FFT data from FftChain
            # For now, this is a placeholder — actual FFT integration
            # requires creating an FftChain reader from the source buffer.
            # The detector and classifier are tested independently with
            # synthetic data.

            # Advance to next window
            self.sweeper.advance()

        logger.info("Scan loop exited")
```

- [ ] **Step 2: Write basic unit test**

Create `test/scanner/test_scanner_service.py`:

```python
import unittest
from unittest.mock import MagicMock, patch


class TestScannerService(unittest.TestCase):
    def test_state_initial(self):
        from owrx.scanner import ScannerState
        state = ScannerState()
        self.assertEqual(state.status, ScannerState.IDLE)
        self.assertEqual(state.current_freq, 0)

    def test_state_update_notifies_listeners(self):
        from owrx.scanner import ScannerState
        state = ScannerState()
        received = []
        state.add_listener(lambda d: received.append(d))
        state.update(status=ScannerState.SCANNING, current_freq=100000000)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["status"], "scanning")
        self.assertEqual(received[0]["current_freq"], 100000000)

    def test_state_to_dict(self):
        from owrx.scanner import ScannerState
        state = ScannerState()
        state.update(status=ScannerState.LISTENING, current_freq=162475000)
        d = state.to_dict()
        self.assertEqual(d["status"], "listening")
        self.assertIn("scan_progress", d)

    def test_hold_and_skip(self):
        from owrx.scanner import ScannerService, ScannerState
        service = ScannerService()
        service.state.update(
            status=ScannerState.SCANNING, current_freq=162475000,
        )
        service.hold()
        self.assertEqual(service._hold_freq, 162475000)
        self.assertEqual(service.state.status, ScannerState.LISTENING)
        service.skip()
        self.assertIsNone(service._hold_freq)
        self.assertEqual(service.state.status, ScannerState.SCANNING)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests**

```bash
python3 -m pytest test/scanner/test_scanner_service.py -v
```

Expected: All 4 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add owrx/scanner/__init__.py test/scanner/test_scanner_service.py
git commit -m "feat(scanner): add ScannerService with scan loop and state management"
```

---

### Task 8: Scanner REST API controller

**Files:**
- Create: `owrx/controllers/scanner.py`
- Modify: `owrx/http.py` (add routes)

Exposes scanner state, detections, and bookmarks as JSON REST endpoints.

- [ ] **Step 1: Create `owrx/controllers/scanner.py`**

```python
import json
from owrx.controllers import Controller


class ScannerApiController(Controller):
    def indexAction(self):
        """GET /api/scanner — scanner state."""
        from owrx.scanner import ScannerService
        service = ScannerService.get_instance()
        self.send_response(
            json.dumps(service.state.to_dict()),
            content_type="application/json",
        )


class ScannerDetectionsController(Controller):
    def indexAction(self):
        """GET /api/scanner/detections — recent detections."""
        from owrx.scanner import ScannerService
        service = ScannerService.get_instance()
        if service.db is None:
            self.send_response(
                json.dumps([]), content_type="application/json",
            )
            return
        limit = int(self.request.query.get("limit", ["50"])[0])
        detections = service.db.get_recent_detections(limit=limit)
        self.send_response(
            json.dumps(detections), content_type="application/json",
        )


class ScannerActiveController(Controller):
    def indexAction(self):
        """GET /api/scanner/active — most active frequencies."""
        from owrx.scanner import ScannerService
        service = ScannerService.get_instance()
        if service.db is None:
            self.send_response(
                json.dumps([]), content_type="application/json",
            )
            return
        hours = int(self.request.query.get("hours", ["24"])[0])
        active = service.db.get_most_active(hours=hours)
        self.send_response(
            json.dumps(active), content_type="application/json",
        )


class ScannerBookmarksController(Controller):
    def indexAction(self):
        """GET /api/scanner/bookmarks — all bookmarks."""
        from owrx.scanner import ScannerService
        service = ScannerService.get_instance()
        if service.db is None:
            self.send_response(
                json.dumps([]), content_type="application/json",
            )
            return
        bookmarks = service.db.get_all_bookmarks()
        self.send_response(
            json.dumps(bookmarks), content_type="application/json",
        )


class ScannerCommandController(Controller):
    def indexAction(self):
        """POST /api/scanner/command — send command to scanner."""
        from owrx.scanner import ScannerService
        service = ScannerService.get_instance()

        body = json.loads(self.get_body().decode())
        command = body.get("command")

        if command == "start":
            # Requires SDR source setup — handled by WebSocket connection
            self.send_response(
                json.dumps({"error": "use WebSocket to start scanner"}),
                content_type="application/json",
            )
        elif command == "stop":
            service.stop()
        elif command == "pause":
            service.pause()
        elif command == "resume":
            service.resume()
        elif command == "skip":
            service.skip()
        elif command == "hold":
            freq = body.get("frequency")
            service.hold(freq)
        else:
            self.send_response(
                json.dumps({"error": f"unknown command: {command}"}),
                content_type="application/json",
            )
            return

        self.send_response(
            json.dumps(service.state.to_dict()),
            content_type="application/json",
        )
```

- [ ] **Step 2: Add routes to `owrx/http.py`**

Add imports at the top of `owrx/http.py`:

```python
from owrx.controllers.scanner import (
    ScannerApiController,
    ScannerDetectionsController,
    ScannerActiveController,
    ScannerBookmarksController,
    ScannerCommandController,
)
```

Add routes inside `Router.__init__()` (after the existing API routes, around line 130):

```python
    StaticRoute("/api/scanner", ScannerApiController),
    StaticRoute("/api/scanner/detections", ScannerDetectionsController),
    StaticRoute("/api/scanner/active", ScannerActiveController),
    StaticRoute("/api/scanner/bookmarks", ScannerBookmarksController),
    StaticRoute("/api/scanner/command", ScannerCommandController, method="POST"),
```

Also add a route for the scanner HTML page:

```python
    StaticRoute("/scanner", ScannerPageController),
```

Where `ScannerPageController` serves `scanner.html` (we'll create this in a later task — for now, a placeholder that returns 200 OK is fine).

- [ ] **Step 3: Verify routes load without errors**

```bash
docker compose -f docker-compose.dev.yml restart
curl -s http://localhost:8073/api/scanner | python3 -m json.tool
```

Expected: JSON with scanner state (status: "idle").

- [ ] **Step 4: Commit**

```bash
git add owrx/controllers/scanner.py owrx/http.py
git commit -m "feat(scanner): add REST API endpoints for scanner state and commands"
```

---

### Task 9: Mobile scanner UI — Activity Feed + Listening View

**Files:**
- Create: `htdocs/scanner.html`
- Create: `htdocs/scanner/app.js`
- Create: `htdocs/scanner/audio.js`
- Create: `htdocs/scanner/views.js`
- Create: `htdocs/scanner/scanner.css`

This is the mobile-first web UI. Built as static files served by OpenWebRX+. Connects via WebSocket for live state and audio, and via REST API for historical data.

This is a large task but produces the visible product. Implementation details are in the spec UI wireframes. The executing agent should use the `frontend-design` skill for this task.

- [ ] **Step 1: Create `htdocs/scanner.html`**

Minimal HTML shell with viewport meta for mobile, links to CSS and JS:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <title>Scanner</title>
    <link rel="stylesheet" href="scanner/scanner.css">
</head>
<body>
    <div id="app">
        <div id="tap-overlay">
            <div class="tap-prompt">Tap to start</div>
        </div>
        <div id="activity-feed" class="screen active"></div>
        <div id="listening-view" class="screen"></div>
        <div id="log-view" class="screen"></div>
        <div id="settings-view" class="screen"></div>
    </div>
    <script src="scanner/audio.js"></script>
    <script src="scanner/views.js"></script>
    <script src="scanner/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `htdocs/scanner/scanner.css`**

Mobile-first CSS. Dark theme (easier on eyes in the dark). Touch-friendly tap targets (min 44px). Full-width layout.

- [ ] **Step 3: Create `htdocs/scanner/audio.js`**

Mobile audio engine: ADPCM decoder (port from OpenWebRX+'s `AudioEngine.js`), Web Audio API initialization with gesture unlock, ring buffer playback. This is the most complex JS file — reuse the ADPCM decoding logic from the existing `AudioEngine.js` and `AudioProcessor.js`.

Key functions:
- `ScannerAudio.init()` — called on tap, creates AudioContext
- `ScannerAudio.processAudioData(data)` — decodes ADPCM, feeds to AudioWorklet
- `ScannerAudio.stop()` — stops playback

- [ ] **Step 4: Create `htdocs/scanner/views.js`**

Four views matching the spec wireframes:
- `ActivityFeedView` — active signals, recent, bookmarks, most active
- `ListeningView` — frequency display, signal meter, nudge controls, skip/hold/save
- `LogView` — historical detections list with filters
- `SettingsView` — scan range, filter, recording, storage config

Each view is a class with `render()`, `show()`, `hide()` methods that manipulate DOM elements.

- [ ] **Step 5: Create `htdocs/scanner/app.js`**

Main app: WebSocket connection, state management, view routing.
- Opens WebSocket to `/ws/` with scanner-specific handshake
- Dispatches incoming messages to views (state updates, audio data)
- Sends commands (skip, hold, nudge) via WebSocket or REST API
- Navigation between views

- [ ] **Step 6: Add static file serving for scanner assets**

Verify that OpenWebRX+'s existing static file handler serves files from `htdocs/scanner/`. Check `owrx/controllers/assets.py` — it should already serve subdirectories of `htdocs/`.

- [ ] **Step 7: Test in mobile browser**

Open `http://localhost:8073/scanner` on phone (via Tailscale or LAN). Verify:
- Page loads, tap overlay appears
- After tap, activity feed shows (empty, scanner not running)
- Responsive layout works on phone width
- No JS console errors

- [ ] **Step 8: Commit**

```bash
git add htdocs/scanner.html htdocs/scanner/
git commit -m "feat(scanner): add mobile scanner UI with activity feed and listening view"
```

---

### Task 10: Wire scanner service to OpenWebRX+ SDR source + FFT

**Files:**
- Modify: `owrx/scanner/__init__.py`
- Modify: `owrx/connection.py` (add scanner WebSocket handling)

This is the integration task — connecting the scanner service to actual SDR hardware via OpenWebRX+'s APIs.

- [ ] **Step 1: Add FFT reader to ScannerService**

In `owrx/scanner/__init__.py`, add a method that creates an `FftChain` from the SDR source buffer and reads power spectrum data. Reference: `owrx/fft.py:40-73` (SpectrumThread pattern).

```python
from csdr.chain.fft import FftChain

def _create_fft_reader(self):
    """Create an FFT chain reader for the SDR source buffer."""
    pm = Config.get()
    self._fft_chain = FftChain(
        samp_rate=self._sdr_source.getProps()["samp_rate"],
        fft_size=pm["fft_size"],
        fft_fps=10,  # 10 FFT frames per second
        fft_voverlap_factor=0.0,
        fft_compression="none",  # raw power data, no ADPCM
    )
    self._fft_reader = self._sdr_source.getBuffer().getReader()
    # Start pumping IQ data through FFT chain
    # Output is raw power spectrum arrays
```

Note: The exact integration depends on pycsdr's `FftChain` API — the implementing agent should read `csdr/chain/fft.py` and `owrx/fft.py` to understand the correct pump/read pattern.

- [ ] **Step 2: Update scan loop to read real FFT data**

Replace the TODO placeholder in `_scan_loop()` with actual FFT reading, signal detection, classification, and logging.

- [ ] **Step 3: Add scanner WebSocket message handling**

In `owrx/connection.py`, add handling for scanner-specific WebSocket messages:
- `{"type": "scanner_start"}` — starts the scanner with the current SDR source
- `{"type": "scanner_stop"}` — stops the scanner
- `{"type": "scanner_command", "command": "skip"|"hold"|"pause"|"resume"}`
- `{"type": "scanner_nudge", "freq_offset": ±N, "bw_offset": ±N}`

Also add scanner state broadcasting — when `ScannerState` updates, send to connected scanner clients.

- [ ] **Step 4: Test with live SDR hardware**

```bash
docker compose -f docker-compose.dev.yml up -d
# Open http://localhost:8073/scanner on phone
# Tap to start, then start scanner from UI
# Should see scan progress, signal detections appearing
```

- [ ] **Step 5: Commit**

```bash
git add owrx/scanner/__init__.py owrx/connection.py
git commit -m "feat(scanner): wire scanner service to SDR source and FFT pipeline"
```

---

### Task 11: Audio recording service

**Files:**
- Create: `owrx/scanner/recorder.py`
- Create: `test/scanner/test_recorder.py`

Records audio to Opus/OGG files when scanner stops on a signal. Uses ffmpeg for encoding.

- [ ] **Step 1: Write failing tests**

Create `test/scanner/test_recorder.py`:

```python
import unittest
import tempfile
import os
import numpy as np


class TestScannerRecorder(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_recording_creates_file(self):
        from owrx.scanner.recorder import ScannerRecorder
        recorder = ScannerRecorder(
            storage_path=self.tmpdir,
            sample_rate=12000,
        )
        recorder.start_recording(frequency_hz=162475000, mode="nfm")
        # Feed some audio samples
        samples = np.random.randn(12000).astype(np.float32)
        recorder.write_samples(samples)
        path = recorder.stop_recording()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".ogg"))

    def test_storage_pruning(self):
        from owrx.scanner.recorder import ScannerRecorder
        recorder = ScannerRecorder(
            storage_path=self.tmpdir,
            sample_rate=12000,
            max_storage_mb=0,  # immediately over limit
        )
        # Create a dummy recording file
        dummy = os.path.join(self.tmpdir, "old_recording.ogg")
        with open(dummy, "wb") as f:
            f.write(b"\x00" * 1024)
        pruned = recorder.prune()
        self.assertGreater(pruned, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement `owrx/scanner/recorder.py`**

Uses `subprocess` to pipe raw PCM to `ffmpeg` for Opus encoding. Manages storage limits and pruning.

- [ ] **Step 3: Run tests**

```bash
python3 -m pytest test/scanner/test_recorder.py -v
```

- [ ] **Step 4: Commit**

```bash
git add owrx/scanner/recorder.py test/scanner/test_recorder.py
git commit -m "feat(scanner): add audio recorder with Opus encoding and storage management"
```

---

### Task 12: Integration testing with IQ FileSource

**Files:**
- Create: `test/integration/test_scanner_integration.py`

End-to-end test: start scanner with IQ FileSource, verify it detects signals and logs them. Requires the IQ FileSource from PR #6 to be merged.

- [ ] **Step 1: Cherry-pick IQ FileSource**

```bash
git cherry-pick <commit-sha-from-pr-6>
# Or merge the branch if it's clean
```

- [ ] **Step 2: Write integration test**

```python
import unittest
import tempfile
import os


class TestScannerIntegration(unittest.TestCase):
    """Integration test: scanner with IQ FileSource.

    Requires test IQ files with known signals.
    """

    def test_scanner_detects_fm_signal(self):
        """Scanner should detect FM signal in IQ file."""
        # This test uses the generate_test_iq.py tool to create
        # a test file, then configures FileSource to play it,
        # starts the scanner, and verifies detections in the DB.
        pass  # TODO: implement when FileSource is available


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run full test suite**

```bash
python3 -m pytest test/scanner/ -v
```

- [ ] **Step 4: Commit**

```bash
git add test/integration/
git commit -m "test(scanner): add integration test skeleton with IQ FileSource"
```

---

### Task 13: Push, update PR, final verification

- [ ] **Step 1: Run all tests**

```bash
python3 -m pytest test/scanner/ -v
```

- [ ] **Step 2: Push branch**

```bash
git push -u origin feat/scanner-v2
```

- [ ] **Step 3: Create PR on OpenWebRX+ fork**

```bash
gh pr create --repo mihow/openwebrxplus \
    --title "feat: add signal-driven scanner with mobile UI" \
    --body "..."
```

- [ ] **Step 4: Update pi-sdr PR #3 with implementation status**

Update the PR description on mihow/pi-sdr#3 to link to the OpenWebRX+ PR and mark research as complete, implementation in progress.
