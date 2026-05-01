# DHN Client: Synthetic UDP Acquisition Bench

This project generates synthetic electrophysiology data using SpikeInterface, sends it as **pure headerless UDP** to DHN-AQ, records it as MED, and verifies the MED output with `dhn-med-py`. The first milestone covers only the synthetic-data path. LSL is optional and is never placed in front of DHN-AQ.

---

## System Architecture

```
SpikeInterface synthetic recording
    └─> Python headerless UDP sender  (dhn-si-udp)
            └─> DHN-AQ receiver
                    └─> MED output directory
                            └─> dhn-med-py verification  (dhn-verify-med)

Optional (Milestone 2, not required):
    └─> LSL outlet  ──> MNE-LSL realtime analysis
```

**DHN-AQ receives pure UDP — no custom application header.** Channel count, sample rate, dtype, and channel order are configured inside DHN-AQ, not in the payload.

---

## Requirements

- Linux workstation (Ubuntu 22.04+ recommended)
- Python 3.11
- [`uv`](https://github.com/astral-sh/uv) — fast Python package manager
- DHN-AQ installed and configured separately on this machine or reachable over the network
- `tcpdump` — to prove packets leave the NIC before blaming DHN-AQ

---

## Install

```bash
chmod +x scripts/install_dev.sh
./scripts/install_dev.sh

# Activate the virtual environment for subsequent terminal sessions:
source .venv/bin/activate
```

The script installs system packages (requires `sudo`), creates a Python 3.11 virtualenv with `uv`, installs all runtime and dev dependencies, and verifies key imports.

---

## Configure DHN-AQ

DHN-AQ must be configured to **match the sender exactly**. Mismatches cause silent data corruption or no output.

| DHN-AQ setting         | Must match                              |
|------------------------|-----------------------------------------|
| Receive interface/IP   | NIC address the sender targets          |
| UDP port               | `configs/synthetic_udp_16ch.yaml → udp.port` (default 26090) |
| Channel count          | `signal.channels` (default 16)          |
| Sample rate            | `signal.sample_rate_hz` (default 32000 Hz) |
| Data type              | int16, little-endian                    |
| Layout                 | sample-major (frame0_ch0, frame0_ch1, … frame1_ch0, …) |
| Output directory       | wherever you want MED written           |

**Before running:** assign the expected IP to your NIC if needed:

```bash
sudo ip addr add 192.168.3.50/24 dev eno1
ip addr && ip route    # confirm
```

---

## Run Local UDP Probe Test

Use this to confirm packets are leaving the sender **before involving DHN-AQ**.

**Terminal 1 — receiver:**
```bash
dhn-udp-probe --host 0.0.0.0 --port 26090 --expected-bytes 32 --show-samples 4
```

**Terminal 2 — sender:**
```bash
dhn-si-udp --host 127.0.0.1 --port 26090 \
    --channels 16 --sample-rate 32000 \
    --duration 10 --frames-per-packet 1
```

Pass criteria:
- Probe prints packet rate ≈ 32000 pkt/s (one frame per packet at 32 kHz)
- No sender crash
- `Last payload (bytes)` = 32 (16 channels × 2 bytes)

---

## Run DHN-AQ Recording Test

### Step-by-step

1. Configure DHN-AQ (see above).
2. Assign NIC IP if needed.
3. **Terminal 1 — watch packets on the wire:**
   ```bash
   sudo tcpdump -ni eno1 udp port 26090 -vv
   ```
4. **Start DHN-AQ recording.**
5. **Terminal 2 — run synthetic sender:**
   ```bash
   dhn-si-udp --config configs/synthetic_udp_16ch.yaml
   ```
6. After the sender finishes (60 s), **stop DHN-AQ recording cleanly**.

### With a custom config override:
```bash
dhn-si-udp --config configs/synthetic_udp_16ch.yaml --channels 64 --duration 30
```

### Dry-run (no packets sent):
```bash
dhn-si-udp --config configs/synthetic_udp_16ch.yaml --dry-run
```

---

## Verify MED Output

```bash
dhn-verify-med \
    --med-path /mnt/dhn/recordings/<SESSION>.medd \
    --expect-channels 16 \
    --expect-sample-rate 32000 \
    --read-seconds 5 \
    --report-json runs/synthetic_16ch_report.json
```

The verifier checks:
- MED path exists and session opens
- Channel count matches
- Sample rate matches (if metadata exposes it)
- At least 5 seconds of data can be read
- Signal is nonzero
- Per-channel mean/std/min/max are printed in a table

Exit code 0 = all checks pass. Exit code 1 = at least one failure.

---

## Run Unit Tests

Tests do not require DHN-AQ or a network connection.

```bash
pytest tests/ -v
```

Covers:
- `test_scaling.py` — int16 scaling correctness and edge cases
- `test_serialization.py` — sample-major and channel-major byte layout
- `test_config.py` — YAML config loading, defaults, and partial overrides

---

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `tcpdump` sees no packets | Sender network config or wrong NIC/IP |
| Packets visible in `tcpdump` but no MED output | DHN-AQ config mismatch (port, IP, channel count) |
| MED exists but data is scrambled / channels swapped | dtype, layout (sample-major vs channel-major), or channel spec mismatch in DHN-AQ |
| Works at 16 channels, fails at 512 | Packet rate / CPU / NIC tuning; check underrun count in sender output |
| MED verifier reports zero signal | DHN-AQ received packets but recorded silence — check gain/scale settings in DHN-AQ |

**Always start with 16 channels and 1 frame per packet.** Only increase channel count after 16-channel MED readback is confirmed correct.

---

## Channel / Packet Scaling Path

```
16 ch  × 1 frame/pkt  →  32 bytes/pkt   ✓ start here
64 ch  × 1 frame/pkt  →  128 bytes/pkt
128 ch × 1 frame/pkt  →  256 bytes/pkt
512 ch × 1 frame/pkt  →  1024 bytes/pkt  ⚠ near MTU limit
```

For 512-channel configurations, one frame is 1024 bytes — below standard Ethernet MTU. Multiple frames per packet may exceed MTU and cause fragmentation unless jumbo frames are configured on both sides.

---

## Next Milestones

1. **Milestone 2** — Optional LSL mirror: send one copy to DHN-AQ as pure UDP; publish a second copy to an LSL outlet for MNE-LSL realtime analysis.
2. **Milestone 3** — NRD-derived source adapter: extract samples from `.nrd` files (via vendor replay, `nrd2dat`, or custom parser) and reuse the same UDP sender.
3. **Future** — Automated run reports, hospital deployment notes, CI pipeline.
