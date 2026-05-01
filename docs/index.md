# DHN UDP Acquisition — Documentation

## Overview

`darkhorse_stream` is a Python pipeline that streams synthetic neural data over UDP to a DHN-AQ hardware acquisition system, and verifies the recorded output in MED format.

It ships with:

- **Three CLI tools** (`dhn-si-udp`, `dhn-udp-probe`, `dhn-verify-med`)
- **A browser-based operator dashboard** (Django + Channels, WebSocket, Chart.js)
- **REST API** for source selection, waveform preview, file upload, and live status
- **32 automated tests** (pytest)

---

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run migrations (one-time)
cd webapp && python manage.py migrate --run-syncdb

# Start the dashboard
cd webapp && daphne -b 0.0.0.0 -p 8000 dhn_web.asgi:application
# Open http://localhost:8000
```

---

## CLI Tools

### `dhn-si-udp` — Synthetic UDP sender

Generates a 4-channel harmonic sine fingerprint (or uses SpikeInterface) and streams it over UDP as headerless, int16, sample-major packets.

```bash
dhn-si-udp --host 192.168.3.50 --port 26090 --channels 4 --sample-rate 32000 \
           --duration 60 --fundamental-hz 440 --frames-per-packet 1 --dry-run
```

Key flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `192.168.3.50` | Destination IP |
| `--port` | `26090` | Destination UDP port |
| `--channels` | `4` | Number of channels |
| `--sample-rate` | `32000` | Samples / second |
| `--duration` | `60` | Seconds to stream |
| `--frames-per-packet` | `1` | Frames bundled per packet |
| `--target-peak` | `8000` | int16 peak amplitude |
| `--dry-run` | false | Skip network send |

---

### `dhn-udp-probe` — Local UDP receiver probe

Binds on localhost and prints packet statistics. Use to sanity-check the sender.

```bash
dhn-udp-probe --host 0.0.0.0 --port 26090 --timeout 10 --show-samples
```

---

### `dhn-verify-med` — MED file verifier

Opens a MED session written by DHN-AQ and verifies channel count, sample rate, and signal content.

```bash
dhn-verify-med --med-path /data/session.medd --expect-channels 4 \
               --expect-sample-rate 32000 --read-seconds 5 --report-json
```

---

## Dashboard

Open `http://localhost:8000` after starting Daphne.

The dashboard is a single-page app with four setup steps:

1. **Select Source** — choose from Harmonic Sine, SpikeInterface, File Replay, or Network Source
2. **Configure Signal** — source-specific parameters; click "Preview Waveform" to see 0.5 s of data before streaming
3. **Network** — destination host/port and receiver bind address
4. **Payload** — int16 peak and socket buffer size

Controls: Start/Stop Receiver → Start/Stop Sender (in that order).

The right panel shows:
- **Waveform Preview** — 0.5 s multi-channel plot from the REST API
- **Sender Stats** — packets/s, Mbit/s, underruns, elapsed time (via WebSocket)
- **Live Power Spectrum** — FFT of received UDP, per channel (via WebSocket)
- **Channel Verification** — per-channel harmonic match pass/fail

---

## REST API

Full documentation: [api-smoke-tests.md](api-smoke-tests.md)

Base URL: `http://localhost:8000`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sources/` | List available source types |
| POST | `/api/preview/` | Generate 0.5 s waveform preview |
| GET | `/api/files/?dir=uploads` | List uploaded files |
| POST | `/api/upload/` | Upload a raw binary file |
| GET | `/api/status/` | Sender + receiver running state |

---

## Running Tests

```bash
# All tests (CLI pipeline + API smoke tests)
pytest -v

# API smoke tests only
pytest tests/test_api_smoke.py -v

# CLI pipeline tests only
pytest tests/test_scaling.py tests/test_serialization.py tests/test_config.py -v
```

Expected output: **56 passed** (32 API smoke + 24 pipeline).

---

## UDP Packet Format

| Field | Value |
|-------|-------|
| Header | None (headerless) |
| dtype | int16 |
| Endianness | little-endian |
| Layout | sample-major (all channels for frame 0, then frame 1, …) |
| Default MTU warning | 1400 bytes |

---

## Configuration File

`configs/default.yaml` — loaded via `AppConfig.from_yaml(path)`.

```yaml
session:
  name: "test_session"
udp:
  dest_host: "192.168.3.50"
  dest_port: 26090
signal:
  channels: 4
  sample_rate_hz: 32000
  fundamental_hz: 440
payload:
  frames_per_packet: 1
  target_peak: 8000
```
