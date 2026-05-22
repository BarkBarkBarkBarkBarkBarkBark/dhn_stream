"""
Reader for `.nrd` files produced by Neuralynx Cheetah/Pegasus.

Layout on disk:
    [ASCII Neuralynx Data File Header]   (typically 16384 bytes, ###...###)
    [NRD UDP packets back-to-back]       each = 72 + 4*N_channels bytes

The packet binary is the same wire format `dhn-stream` emits, except the
real NRD `size_word` convention is `10 + N` (not `14 + N`). Channel count
and packet boundary are auto-detected by scanning for two consecutive STX
sync words and measuring their byte distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from darkhorse_neuralynx.udp_raw.nrd_sender import (
    NRD_HEADER_SIZE_BYTES,
    NRD_STX,
)

_STX_BYTES = NRD_STX.to_bytes(4, "little")  # b"\x00\x08\x00\x00"


@dataclass(frozen=True)
class NrdFileLayout:
    path: Path
    file_size: int
    header_bytes: int        # offset of first NRD packet (end of ASCII text header)
    packet_size: int         # bytes per packet on disk
    n_channels: int          # samples per packet
    packet_count: int        # (file_size - header_bytes) // packet_size

    @property
    def duration_seconds(self) -> float:
        # Caller supplies sample_rate; this is a packet-count-only helper.
        return float(self.packet_count)


def detect_nrd_file(
    path: str | Path,
    *,
    scan_bytes: int = 1 << 16,
    n_validate: int = 8,
) -> NrdFileLayout:
    """
    Probe a `.nrd` file and return its packet layout.

    - Scans the first `scan_bytes` for the STX sync word `00 08 00 00`.
    - Measures the byte distance to the next STX → `packet_size`.
    - Validates by checking that the next `n_validate` packet boundaries
      also begin with STX.
    - Derives `n_channels = (packet_size - 72) / 4`.
    """
    p = Path(path)
    file_size = p.stat().st_size
    with p.open("rb") as f:
        head = f.read(scan_bytes)

    first = head.find(_STX_BYTES)
    if first < 0:
        raise ValueError(f"No NRD STX sync word found in first {scan_bytes} bytes of {p}")
    second = head.find(_STX_BYTES, first + 4)
    if second < 0:
        raise ValueError(f"Only one STX found in first {scan_bytes} bytes; cannot determine packet size")

    packet_size = second - first
    if packet_size < (NRD_HEADER_SIZE_BYTES + 4):
        raise ValueError(f"Implausible packet size {packet_size} (too small for any NRD packet)")
    payload_bytes = packet_size - NRD_HEADER_SIZE_BYTES - 4
    if payload_bytes <= 0 or payload_bytes % 4 != 0:
        raise ValueError(f"packet size {packet_size} not consistent with int32 samples")
    n_channels = payload_bytes // 4

    # Validate the next few boundaries
    with p.open("rb") as f:
        for k in range(n_validate):
            f.seek(first + (k + 1) * packet_size)
            sig = f.read(4)
            if sig and sig != _STX_BYTES:
                raise ValueError(
                    f"STX missing at packet boundary #{k + 1} "
                    f"(offset {first + (k + 1) * packet_size}); got {sig.hex()}"
                )

    packet_count = (file_size - first) // packet_size
    return NrdFileLayout(
        path=p,
        file_size=file_size,
        header_bytes=first,
        packet_size=packet_size,
        n_channels=n_channels,
        packet_count=packet_count,
    )


def iter_nrd_samples(
    layout: NrdFileLayout,
    *,
    batch_packets: int = 1024,
) -> Iterator[np.ndarray]:
    """
    Yield `(batch_frames, n_channels)` int32 arrays of samples from the file.

    Reads packets in chunks of `batch_packets`, parses out only the int32
    sample payload (offset 68, length `4 * n_channels`), and rebuilds a
    contiguous (frames, channels) array. Header/CRC bytes are discarded.

    Stops at EOF so a replay represents one pass through the source file.
    """
    n = layout.n_channels
    pkt_size = layout.packet_size
    sample_off = NRD_HEADER_SIZE_BYTES
    sample_bytes = 4 * n

    chunk_bytes = batch_packets * pkt_size
    with layout.path.open("rb") as f:
        f.seek(layout.header_bytes)
        while True:
            buf = f.read(chunk_bytes)
            if not buf:
                return
            n_pkts = len(buf) // pkt_size
            if n_pkts == 0:
                return
            # Reshape to (n_pkts, pkt_size) and slice out the samples region
            view = np.frombuffer(buf[: n_pkts * pkt_size], dtype=np.uint8).reshape(n_pkts, pkt_size)
            samples = view[:, sample_off : sample_off + sample_bytes].reshape(n_pkts, n, 4)
            # Reinterpret each (n, 4) row as int32; little-endian on disk.
            chunk_i32 = samples.view("<i4").reshape(n_pkts, n)
            yield np.ascontiguousarray(chunk_i32)
