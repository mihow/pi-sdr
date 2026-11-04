# docs/claude index

Searchable index of design and agent docs. Last indexed 2026-11-04.

| Path | What it holds | Keywords |
|---|---|---|
| `docs/superpowers/specs/2026-11-03-sdr-scanner-design.md` | `sdrscan` design: architecture, schema (section 7), digest rules (8), clip quota (9), uplink budget (10), dashboard (11), deployment (12), open measurements (16). | scanner, digest, quota, ledger, dashboard, governor |
| `docs/superpowers/plans/2026-11-03-sdr-scanner.md` | Task plan A1 to B12 with test lists and verification commands (arm64 build, qemu subset). | plan, TDD, arm64, qemu, install kit |
| `docs/claude/research/2026-04-11-scanner-signal-mining.md` | v1 scanner log analysis: per-band floors differ 16 to 17 dB, ADC clip ceiling, the four stacked v1 bugs. | v1, squelch, AGC, VAD rate bug |
| `scanner/README.md` | How to simulate and view the dashboard, measured byte budget, deploy steps, open measurements. | simulate, bytes per view, install.sh |

Code map for `scanner/sdrscan/` (symbols, not lines):

- Scan loop: `runner.Runner._dwell`, `Runner._audio` (gate and clip writer), `Runner._close_tx`.
- Whole service: `service.Service.tick` (health, STT, hourly `_finish_hour`), `Service.watchdog_check`.
- Voice: `voice.gate.VoiceGate._classify`; STT: `voice.stt.WhisperCppStt.argv`.
- Clips: `clips.ClipStore.enforce` (tier, per-channel and free-space rules).
- Rollups: `rollup._split` (shared by incremental and recompute), `rollup.query_window`.
- Digest: `summarize.build_digest`, `_Baseline` (unusual and quiet), golden `tests/golden/digest_week.json` (regenerate with `UPDATE_GOLDEN=1`).
- Uplink: `uplink.DigestPublisher.publish` (cadence, outbox coalescing), `uplink.Ledger`.
- Dashboard: `web/index.html.tmpl`, `dashboard.render_page`; live server `live.LiveServer._get`.

Gotchas found while building Part B:

- `webrtcvad` 2.0.10 imports `pkg_resources`, which new setuptools removed; the gate imports `_webrtcvad` directly.
- A synthetic scene whose noise floor is below one 8-bit step makes quantization turn a strong carrier plus the DC spike into a mirror image; the end-to-end scene sets `quantize: false`.
- The build host's disk is above 85% full, so a real `statvfs` triggers clip eviction (15% floor); clip tests use `FakeFs`.
- Container processes are outside the systemd unit's cgroup, so `WatchdogSec` and `CPUQuota` on the unit do not reach them; the unit passes docker run limits and the service has its own progress watchdog.
