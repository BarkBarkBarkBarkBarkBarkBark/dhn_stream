# DHN NRD Streamer

Minimum-viable command-line tools for sending Neuralynx NRD UDP packets into `DHN_Acq`.

```text
synthetic source  →  dhn-stream stream  →  NRD UDP packets on :26090  →  DHN_Acq  →  MED
```

Three commands:

| Command            | Purpose                                                    |
|--------------------|------------------------------------------------------------|
| `dhn-stream`       | Emit NRD UDP packets (harmonic or SpikeInterface source)   |
| `dhn-probe`        | Bind a UDP port and report packet/byte rate (sanity check) |
| `dhn-verify-med`   | Verify a recorded MED session matches expectations         |

Format reference: [docs/nrd-format.md](docs/nrd-format.md).

## Install

```bash
cd /home/dhn/Documents/development/dhn_stream
chmod +x scripts/install_dev.sh
./scripts/install_dev.sh
source .venv/bin/activate

# Optional extras
uv pip install -e '.[spike]'   # SpikeInterface synthetic source
uv pip install -e '.[verify]'  # dhn-verify-med
uv pip install -e '.[dashboard]' # browser waveform/metrics inspector
uv pip install -e '.[dev]'     # pytest / ruff / mypy
```

Core dependencies are `numpy`, `typer`, `rich`. Everything else is opt-in.

## Quick Start: Harmonic NRD Stream

```bash
dhn-stream stream --host 127.0.0.1 --channels 4 --sample-rate 32000 --peak 100000
```

For a remote DHN_Acq host:

```bash
dhn-stream stream --host 192.168.3.50 --channels 4 --sample-rate 32000 --peak 100000
```

Packet size for 4 channels = `68 + 4*4 + 4 = 88 bytes`. `DHN_Acq` should report `32000 Hz` and the configured channel count.

## SpikeInterface Source

```bash
dhn-stream stream \
  --source spikeinterface \
  --channels 8 --units 8 --sample-rate 32000 \
  --recording-seconds 30 \
  --peak 100000 --noise-std 5000 \
  --seed 42
```

Tuning amplitudes (NRD samples are 24-bit-style ADC counts, FS ≈ ±8.4M):

| Look       | `--peak` | `--noise-std` |
|------------|----------|---------------|
| subtle     | 50_000   | 2_500         |
| default    | 100_000  | 5_000         |
| prominent  | 400_000  | 15_000        |
| loud       | 2_000_000 | 50_000       |

Scaling: `--peak` maps to the **spike-peak** amplitude (99.99th percentile of |traces|), so spikes show clearly above the noise floor on `DHN_Acq`'s ~24-bit ADC display.

## Replay a Real `.nrd` File

Channel count is auto-detected from the file (by measuring the byte distance between the first two STX sync words). Override with `--channels` if needed.

```bash
dhn-stream stream \
  --source nrd-file --file /mnt/MED_Data_iSSD/RawData.nrd \
  --host 127.0.0.1 --sample-rate 32000
```

Replay stops at EOF and prints `Yayy!! Replay Done!`. Use `--duration` to stop earlier. The original file packet IDs and timestamps are discarded; the sender emits its own monotonic IDs and fractional-µs timestamps so DHN_Acq reports the rate cleanly. Throughput at 512 ch / 32 kHz is ~540 Mbit/s — verify your NIC and `--send-buffer` (default 8 MB) can keep up.

### Prime DHN_Acq Before Replaying

Use `--prime-until-enter` when DHN_Acq needs to see a live, correctly shaped NRD stream before you start recording. The streamer first sends zero-valued packets with the channel count detected from the `.nrd` file. After DHN_Acq is recording, return to the terminal and press Enter; the same sender continues with the real file replay without resetting packet IDs or timestamps.

```bash
dhn-stream stream \
  --source nrd-file --file /mnt/MED_Data_iSSD/DA-075-RawData.nrd \
  --host 127.0.0.1 --sample-rate 32000 \
  --prime-until-enter
```

The captured output will contain a zero pre-roll followed by the real replay. That pre-roll is intentional; it keeps the acquisition session alive early enough that comparisons against the original uncompressed `.nrd` do not lose the first seconds of real data.

To inspect the stream in the dashboard while DHN_Acq receives the main packets, mirror a copy to a separate UDP port:

```bash
dhn-stream stream \
  --source nrd-file --file /mnt/MED_Data_iSSD/DA-075-RawData.nrd \
  --host 127.0.0.1 --sample-rate 32000 \
  --prime-until-enter \
  --mirror-host 127.0.0.1 --mirror-port 26091
```

## Live Waveform Dashboard

The optional dashboard is a local browser inspector for packet health, channel metrics, labels, and decimated waveform previews. It listens on a mirror UDP port by default so it does not compete with DHN_Acq for Neuralynx port `26090`.

```bash
dhn-stream dashboard \
  --web-host 127.0.0.1 --web-port 8000 \
  --udp-host 127.0.0.1 --udp-port 26091 \
  --sample-rate 32000 \
  --file /mnt/MED_Data_iSSD/DA-075-RawData.nrd \
  --channel-config /home/dhn/DHN/DHN_Acq/DHN_Acq_cs.csv \
  --connection-map /mnt/MED_Data_iSSD/DA075_connection_map.xlsx
```

Open `http://127.0.0.1:8000`. The dashboard shows packet rate, throughput, parse errors, packet/timestamp gaps, a dense channel grid, quality labels, and a click-through waveform view. Waveforms are min/max decimated for inspection; use the recorded `.nrd`/MED data for lossless comparisons.

## Inspect `.nrd` Signal Statistics

```bash
dhn-stream stats \
  --file /mnt/MED_Data_iSSD/RawData.nrd \
  --sample-rate 32000 \
  --report-json runs/nrd-stats.json
```

The stats command streams through the file and reports per-channel min, max, mean, standard deviation, RMS, mean absolute amplitude, max absolute amplitude, and simple channel-quality labels. Use `--seconds` or `--max-packets` for a quick partial scan of a large file.

```bash
dhn-stream stats --file /mnt/MED_Data_iSSD/RawData.nrd --sample-rate 32000 --seconds 10
```

`flat` channels have near-zero amplitude by the configured thresholds. `noise-like` and `signal-like` are heuristics based on peak/RMS shape, not biological classification. Micro/macro electrode labels are only reported when the `.nrd` header has recognizable per-channel metadata; otherwise they are shown as `unknown`.

## DHN_Acq Receiver Setup

In `DHN_Acq_rc.txt` set:

| Field                          | Value                                  |
|--------------------------------|----------------------------------------|
| `Receiving Server IP Address`  | sender host (e.g. `127.0.0.1`)         |
| `Receiving Port Number`        | `26090`                                |
| `Receive As Broadcast`         | `NO` (or `YES` if broadcasting)        |
| `Network Interface`            | the NIC the stream arrives on          |

Channel count in `DHN_Acq_cs.csv` must match `--channels`.

## Sanity-Check the Stream

```bash
# Receive-side: what's actually arriving on the port?
dhn-probe --port 26090 --expected-bytes 88

# Wire-level: confirm STX 00 08 00 00 in every packet
sudo tcpdump -ni <iface> -XX 'udp port 26090' | head -40
```

## Verify MED Output

```bash
dhn-verify-med \
  --med-path /path/to/session.medd \
  --expect-channels 8 --expect-sample-rate 32000 \
  --read-seconds 5 \
  --report-json runs/report.json
```

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m pytest tests/ -q
```

## Troubleshooting

| Symptom                              | Likely cause                                                                |
|--------------------------------------|-----------------------------------------------------------------------------|
| `DHN_Acq` reports `32258 Hz`         | Old sender; integer-µs timestamp step. Pull latest.                         |
| `DHN_Acq` reports `-2147483648 Hz`   | Receiver is decoding non-NRD bytes. Check `dhn-stream` was actually used.   |
| Traces are flat                      | `--peak` too low for the display.                                           |
| Traces fill the lane vertically      | `--peak` too high; DHN_Acq autoscales per channel.                          |
| `tcpdump` sees nothing               | Wrong interface, host IP, or `DHN_Acq` is bound to a different NIC.         |
| `SpikeInterface` import error        | `uv pip install -e '.[spike]'`.                                             |
