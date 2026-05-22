"""Helpers for the dhn-stream CLI: harmonic chunk generator + status table."""

from __future__ import annotations

import numpy as np
from rich.table import Table


def build_harmonic_chunk(
    n_frames: int,
    n_channels: int,
    sample_rate: int,
    fundamental: float,
    t_offset: float,
) -> np.ndarray:
    """Return (n_frames, n_channels) float64 array in [-1, 1].

    Channel k carries harmonics f₁ through f_{k+1} of `fundamental`, normalised
    per channel so the peak amplitude is 1.0.
    """
    t = np.arange(n_frames, dtype=np.float64) / sample_rate + t_offset
    out = np.zeros((n_frames, n_channels), dtype=np.float64)
    for ch in range(n_channels):
        n_harmonics = ch + 1
        for h in range(1, n_harmonics + 1):
            out[:, ch] += np.sin(2.0 * np.pi * fundamental * h * t)
        peak = np.max(np.abs(out[:, ch])) or 1.0
        out[:, ch] /= peak
    return out


def status_table(
    elapsed: float,
    duration: float,
    packets_sent: int,
    bytes_sent: int,
    underruns: int,
    total_packets: int,
    total_bytes: int,
    total_underruns: int,
    channels: int,
    sr: int,
    fundamental: float,
    payload_bytes: int,
    source_label: str = "harmonic sine",
) -> Table:
    """Render the live status table for `dhn-stream stream`."""
    del packets_sent, bytes_sent, underruns  # last-chunk stats unused; kept for future
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", min_width=26)
    t.add_column("v")
    dur_str = f"{duration:.0f} s" if duration > 0 else "∞  (Ctrl-C to stop)"
    t.add_row("Elapsed", f"{elapsed:.1f} s / {dur_str}")
    if source_label == "harmonic sine":
        t.add_row("Channels", f"{channels}  (f₁={fundamental} Hz, each ch adds 1 harmonic)")
    else:
        t.add_row("Channels", f"{channels}  ({source_label})")
    t.add_row("Sample rate", f"{sr} Hz")
    t.add_row("Payload bytes / pkt", f"{payload_bytes} bytes  (1 frame/packet, NRD)")
    elapsed_safe = max(elapsed, 1e-9)
    packet_rate = total_packets / elapsed_safe
    throughput_mbps = (total_bytes * 8) / (elapsed_safe * 1_000_000)
    t.add_row("Packet rate", f"{packet_rate:.0f} pkt/s")
    t.add_row("Throughput", f"{throughput_mbps:.4f} Mbit/s")
    t.add_row("Total packets", f"{total_packets:,}")
    t.add_row("Total bytes", f"{total_bytes:,}")
    t.add_row("Underruns", str(total_underruns))
    return t
