# Scanner V2 Research Session — 2026-03-23

## Goal

Validate the scanner v2 design spec against OpenWebRX+ internals. Answer the open questions from the spec by reading the actual source code.

## Open Questions to Answer

1. **Server-side extension:** Does OpenWebRX+ have a plugin API? Can we add a scanner plugin without forking?
2. **SDR retune API:** Can we retune center frequency programmatically from Python?
3. **FFT data access:** How do we read FFT power spectrum server-side?
4. **Audio tee for recording:** Can we tap the demod audio for recording?
5. **Mobile audio:** WebSocket audio streaming architecture.

## Resources

- OpenWebRX+ fork: `/home/michael/Projects/Radio/OpenWebRX/openwebrx+`
- Fork PRs: https://github.com/mihow/openwebrxplus/pulls
- Key PRs to study:
  - PR #6: IQ FileSource for automated testing
  - PR #5: Signal Classification Plugin
  - PR #7: SDR integration tests
  - PR #3: LoRa demodulator
  - PR #10: PyTorch signal classification

## Findings

### 1. Server-side extension points
_pending_

### 2. SDR retune API
_pending_

### 3. FFT data access
_pending_

### 4. Audio recording tap
_pending_

### 5. WebSocket audio architecture
_pending_

### 6. Existing PRs — relevant patterns
_pending_

## Decisions & Follow-ups
_pending_
