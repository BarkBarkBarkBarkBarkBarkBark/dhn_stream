# Neuralynx NRD UDP wire format — DHN_Acq integration reference

This is the hospital-deployment cheat sheet for the wire format that
`dhn-stream` emits and that `DHN_Acq` decodes. Source of truth: this
document plus [`smoke_tests.yaml`](smoke_tests.yaml) plus the implementation
in [`src/darkhorse_neuralynx/udp_raw/nrd_sender.py`](../src/darkhorse_neuralynx/udp_raw/nrd_sender.py).

## Headline insight

**One UDP datagram per sample timestep, carrying every channel as int32.**

Not one packet per channel. Not batched. The receiver (DHN_Acq) infers the
sample rate from the *packet rate*, so the sender must emit packets at
exactly `sample_rate_hz` per second.

## Packet layout

Little-endian throughout. Total size: `68 + 4·N + 4` bytes for `N` channels.

| Offset | Size  | Type        | Field                          |
|-------:|------:|-------------|--------------------------------|
|      0 |     4 | `uint32`    | `stx` = `0x00000800`           |
|      4 |     4 | `int32`     | `packet_id` (monotonic +1)     |
|      8 |     4 | `int32`     | `size_int32_words` = `14 + N`  |
|     12 |     4 | `uint32`    | `timestamp_high` (µs, hi 32)   |
|     16 |     4 | `uint32`    | `timestamp_low`  (µs, lo 32)   |
|     20 |     4 | `int32`     | `status`                       |
|     24 |     4 | `uint32`    | `parallel_port` (TTL bits)     |
|     28 |    40 | `int32[10]` | `extras` (board reserved)      |
|     68 |  4·N  | `int32[N]`  | `samples` (one per channel)    |
| 68+4·N |     4 | `uint32`    | `crc` (XOR of preceding int32) |

`stx` on the wire is the four bytes `00 08 00 00` (STX little-endian).

## Sample units

Samples are int32 ADC counts. DHN_Acq displays the full 24-bit envelope, so
the useful range is roughly `±8_388_608` counts. Practical synthetic values:

| Use case               | Peak counts | Noise σ counts |
|------------------------|------------:|---------------:|
| Subtle trace           |      50_000 |          2_500 |
| Default visible spikes |     100_000 |          5_000 |
| Prominent              |     400_000 |         15_000 |
| Loud / saturate        |   2_000_000 |         50_000 |

DHN_Acq autoscales per channel and renders min/max per pixel column. A
broadband signal will always fill the lane vertically. Sparse spike sources
produce thin noise bands with visible spike excursions.

## Pacing — why the fractional µs accumulator matters

`1_000_000 / 32_000 = 31.25 µs`. Storing the timestamp step as an integer
(`31 µs`) makes DHN_Acq compute `1e6 / 31 ≈ 32258 Hz`. The implementation
accumulates the fractional remainder per packet so the int µs timestamps
track the true sample rate over long runs. DHN_Acq then reports `32000 Hz`.

## DHN_Acq receiver settings

In `DHN_Acq_rc.txt`:

| Field                          | Value                                |
|--------------------------------|--------------------------------------|
| `Receiving Server IP Address`  | sender host (e.g. `127.0.0.1`)       |
| `Receiving Port Number`        | `26090`                              |
| `Receive As Broadcast`         | `NO` (or `YES` for broadcast)        |
| `Network Interface`            | NIC where the stream arrives         |

Channel count and channel names in `DHN_Acq_cs.csv` must match the sender's
`--channels` count.

## Hospital deployment caveats

- **MTU**: `68 + 4·N + 4` bytes. At standard 1500-byte MTU, fragmentation
  begins above ~358 channels. For 512 channels (`2120 bytes`) enable jumbo
  frames or accept fragmentation.
- **NIC pinning**: bind both sender and `DHN_Acq` to the same physical NIC.
  Mixed loopback / NIC IPs cause silent drops.
- **Subnet**: sender and DHN_Acq must be reachable on the same subnet, or
  via a router that forwards UDP/26090 (most clinical switches do not).
- **Firewall**: open UDP/26090 in both directions.
- **Broadcast mode**: only set `Receive As Broadcast: YES` when the sender
  emits to the subnet broadcast address (e.g. `192.168.3.255`). Otherwise
  the receiver will silently drop unicast packets.
- **Clock**: DHN_Acq derives sample rate from packet cadence, not from any
  declared rate. Schedulers, GC pauses, or thermal throttling on the sender
  host will appear as drift in the captured rate. For long clinical runs,
  pin the sender process: `chrt -f 50` or use the supplied `renice_helper`.
- **No control channel needed**: the bidirectional Pegasus reference-setup /
  start/stop fiber is *not* required to decode raw NRD UDP. DHN_Acq accepts
  the stream as soon as packets arrive.

## Cross-references

- Implementation: [`udp_raw/nrd_sender.py`](../src/darkhorse_neuralynx/udp_raw/nrd_sender.py)
- Tests: [`tests/test_nrd_packet.py`](../tests/test_nrd_packet.py)
- Smoke spec: [`smoke_tests.yaml`](smoke_tests.yaml)
- Originating reference: Matt @ Neuralynx, April 2026.
