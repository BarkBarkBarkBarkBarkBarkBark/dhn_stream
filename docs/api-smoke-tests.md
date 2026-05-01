# API Smoke Tests

All 32 smoke tests live in [`tests/test_api_smoke.py`](../tests/test_api_smoke.py).

Run them with:

```bash
pytest tests/test_api_smoke.py -v
```

---

## Endpoint: `GET /api/sources/`

Returns the list of available source types.

**Example:**

```bash
curl http://localhost:8000/api/sources/
```

**Response (200):**

```json
{
  "sources": [
    {
      "id": "sine_harmonics",
      "label": "Synthetic — Harmonic Sine Waves",
      "description": "...",
      "params": ["fundamental_hz", "channels", "sample_rate_hz", "frames_per_packet", "target_peak"]
    },
    ...
  ]
}
```

**Tests:**

| Test | Assertion |
|------|-----------|
| `test_returns_200` | HTTP 200 |
| `test_has_sources_key` | Response has `sources` key |
| `test_has_four_sources` | Exactly 4 sources |
| `test_source_ids` | IDs = `{sine_harmonics, spikeinterface, udp_passthrough, file_replay}` |
| `test_each_source_has_required_fields` | Every source has `id`, `label`, `description`, `params` |
| `test_post_not_allowed` | POST → HTTP 405 |

---

## Endpoint: `POST /api/preview/`

Generates a 0.5 s waveform preview for the given source and config.

**Request body (JSON):**

```json
{
  "source": "sine_harmonics",
  "config": {
    "channels": 4,
    "sample_rate_hz": 32000,
    "fundamental_hz": 440
  }
}
```

**Response (200):**

```json
{
  "time_s": [0.0, 3.125e-5, ...],
  "channels": [[...], [...], [...], [...]],
  "channel_labels": ["Ch 0  (440 Hz)", "Ch 1  (440 Hz + 880 Hz)", ...],
  "sample_rate_hz": 32000,
  "preview_seconds": 0.5,
  "properties": {
    "fundamental_hz": 440,
    "n_harmonics_per_channel": [1, 2, 3, 4],
    "dtype": "int16",
    "layout": "sample_major"
  }
}
```

**curl examples:**

```bash
# Sine harmonics
curl -X POST http://localhost:8000/api/preview/ \
  -H "Content-Type: application/json" \
  -d '{"source":"sine_harmonics","config":{"channels":4,"sample_rate_hz":32000,"fundamental_hz":440}}'

# SpikeInterface
curl -X POST http://localhost:8000/api/preview/ \
  -H "Content-Type: application/json" \
  -d '{"source":"spikeinterface","config":{"channels":4,"units":4,"seed":42,"sample_rate_hz":16000}}'

# File replay (must upload file first)
curl -X POST http://localhost:8000/api/preview/ \
  -H "Content-Type: application/json" \
  -d '{"source":"file_replay","config":{"file_path":"/abs/path/data.bin","channels":4,"sample_rate_hz":32000}}'
```

**Tests:**

| Test | Assertion |
|------|-----------|
| `test_sine_returns_200` | HTTP 200 |
| `test_sine_response_shape` | All 6 keys present |
| `test_sine_channel_count` | `len(channels) == 4` |
| `test_sine_time_length` | `len(time_s) == sr * 0.5` |
| `test_sine_normalised_to_minus1_plus1` | All samples in `[-1, 1]` |
| `test_spikeinterface_returns_200` | HTTP 200 |
| `test_spikeinterface_has_channels` | `len(channels) == 4` |
| `test_unknown_source_returns_400` | Unknown source → HTTP 400 |
| `test_invalid_json_returns_400` | Non-JSON body → HTTP 400 |
| `test_file_replay_missing_file_returns_500` | Nonexistent path → HTTP 500 |
| `test_get_not_allowed` | GET → HTTP 405 |

---

## Endpoint: `GET /api/files/?dir=<name>`

Lists files in a whitelisted directory.

Allowed `dir` values: `uploads`, `configs`

**Example:**

```bash
curl http://localhost:8000/api/files/?dir=uploads
```

**Response (200):**

```json
{
  "directory": "/home/marco/darkhorse_stream/uploads",
  "entries": [
    {
      "name": "recording.bin",
      "path": "/home/marco/darkhorse_stream/uploads/recording.bin",
      "size_bytes": 131072,
      "is_dir": false,
      "extension": ".bin"
    }
  ]
}
```

**Tests:**

| Test | Assertion |
|------|-----------|
| `test_uploads_returns_200` | HTTP 200 |
| `test_uploads_has_entries_key` | Has `entries` and `directory` keys |
| `test_invalid_dir_returns_400` | Path-traversal attempt → HTTP 400 |
| `test_unknown_dir_returns_400` | Unlisted dir name → HTTP 400 |
| `test_post_not_allowed` | POST → HTTP 405 |

---

## Endpoint: `POST /api/upload/`

Uploads a raw binary file (int16 little-endian).

Accepted extensions: `.bin`, `.dat`, `.raw`, `.int16`

**Example:**

```bash
curl -X POST http://localhost:8000/api/upload/ \
  -F "file=@/path/to/recording.bin"
```

**Response (200):**

```json
{
  "path": "/home/marco/darkhorse_stream/uploads/recording.bin",
  "filename": "recording.bin",
  "size_bytes": 131072
}
```

**Tests:**

| Test | Assertion |
|------|-----------|
| `test_no_file_returns_400` | No file field → HTTP 400 |
| `test_wrong_extension_returns_400` | `.mp3` → HTTP 400 |
| `test_valid_bin_upload` | `.bin` → HTTP 200, correct `size_bytes` |
| `test_get_not_allowed` | GET → HTTP 405 |

---

## Endpoint: `GET /api/status/`

Returns the running state of the background sender and receiver threads.

**Example:**

```bash
curl http://localhost:8000/api/status/
```

**Response (200):**

```json
{
  "sender": {
    "running": false,
    "stats": null
  },
  "receiver": {
    "running": false,
    "spectrum": null
  }
}
```

When threads are running, `stats` and `spectrum` are populated with the latest push from the WebSocket consumer.

**Tests:**

| Test | Assertion |
|------|-----------|
| `test_returns_200` | HTTP 200 |
| `test_has_sender_and_receiver` | Both top-level keys present |
| `test_sender_has_running_key` | `sender.running` present |
| `test_receiver_has_running_key` | `receiver.running` present |
| `test_both_stopped_on_boot` | Both `false` at startup |
| `test_post_not_allowed` | POST → HTTP 405 |
