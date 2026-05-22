"""Streaming statistics for Neuralynx NRD files."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from darkhorse_neuralynx.udp_raw.nrd_file import NrdFileLayout, detect_nrd_file, iter_nrd_samples


@dataclass(frozen=True)
class NrdChannelStats:
    channel: int
    count: int
    minimum: int
    maximum: int
    mean: float
    std: float
    rms: float
    mean_abs: float
    max_abs: int
    peak_to_rms: float
    quality: str
    electrode_type: str


@dataclass(frozen=True)
class NrdStatsReport:
    path: str
    file_size: int
    header_bytes: int
    packet_size: int
    n_channels: int
    packets_in_file: int
    packets_analyzed: int
    sample_rate_hz: int | None
    estimated_duration_seconds: float | None
    header_mentions_micro: bool
    header_mentions_macro: bool
    channels: list[NrdChannelStats]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_header_text(layout: NrdFileLayout) -> str:
    if layout.header_bytes <= 0:
        return ""
    with layout.path.open("rb") as handle:
        raw = handle.read(layout.header_bytes)
    return raw.decode("utf-8", errors="ignore")


def _electrode_type_from_header(header_text: str, channel: int) -> str:
    lowered = header_text.lower()
    if not lowered:
        return "unknown"

    channel_tokens = (
        f"ch{channel}",
        f"ch_{channel}",
        f"channel {channel}",
        f"channel={channel}",
        f"channel_number={channel}",
    )
    matching_lines = [line.lower() for line in header_text.splitlines() if any(token in line.lower() for token in channel_tokens)]
    if any("micro" in line for line in matching_lines):
        return "micro"
    if any("macro" in line for line in matching_lines):
        return "macro"
    return "unknown"


def _quality_label(
    max_abs: int,
    std: float,
    peak_to_rms: float,
    flat_peak: int,
    flat_std: float,
    noise_peak_to_rms: float,
) -> str:
    if max_abs <= flat_peak or std <= flat_std:
        return "flat"
    if peak_to_rms <= noise_peak_to_rms:
        return "noise-like"
    return "signal-like"


def compute_nrd_stats(
    path: str | Path,
    *,
    sample_rate_hz: int | None = None,
    max_packets: int | None = None,
    batch_packets: int = 4096,
    flat_peak_threshold: int = 0,
    flat_std_threshold: float = 1.0,
    noise_peak_to_rms_threshold: float = 8.0,
) -> NrdStatsReport:
    """Compute per-channel statistics from an NRD file in streaming batches."""
    layout = detect_nrd_file(path)
    if max_packets is not None and max_packets <= 0:
        raise ValueError("max_packets must be positive when provided")
    if batch_packets <= 0:
        raise ValueError("batch_packets must be positive")

    n_channels = layout.n_channels
    minimum = np.full(n_channels, np.iinfo(np.int32).max, dtype=np.int64)
    maximum = np.full(n_channels, np.iinfo(np.int32).min, dtype=np.int64)
    sum_values = np.zeros(n_channels, dtype=np.float64)
    sum_squares = np.zeros(n_channels, dtype=np.float64)
    sum_abs = np.zeros(n_channels, dtype=np.float64)
    packets_analyzed = 0

    for chunk in iter_nrd_samples(layout, batch_packets=batch_packets):
        if max_packets is not None:
            remaining = max_packets - packets_analyzed
            if remaining <= 0:
                break
            chunk = chunk[:remaining]
        if chunk.size == 0:
            break

        chunk_i64 = chunk.astype(np.int64, copy=False)
        minimum = np.minimum(minimum, chunk_i64.min(axis=0))
        maximum = np.maximum(maximum, chunk_i64.max(axis=0))
        sum_values += chunk_i64.sum(axis=0, dtype=np.float64)
        sum_squares += np.square(chunk_i64, dtype=np.float64).sum(axis=0, dtype=np.float64)
        sum_abs += np.abs(chunk_i64).sum(axis=0, dtype=np.float64)
        packets_analyzed += int(chunk.shape[0])

    if packets_analyzed == 0:
        raise ValueError(f"No packets available to analyze in {layout.path}")

    count = float(packets_analyzed)
    mean = sum_values / count
    variance = np.maximum((sum_squares / count) - np.square(mean), 0.0)
    std = np.sqrt(variance)
    rms = np.sqrt(sum_squares / count)
    mean_abs = sum_abs / count
    max_abs = np.maximum(np.abs(minimum), np.abs(maximum))

    header_text = _read_header_text(layout)
    header_lower = header_text.lower()
    channel_reports: list[NrdChannelStats] = []
    for idx in range(n_channels):
        channel = idx + 1
        channel_rms = float(rms[idx])
        peak_to_rms = float(max_abs[idx] / channel_rms) if channel_rms > 0 else 0.0
        quality = _quality_label(
            int(max_abs[idx]),
            float(std[idx]),
            peak_to_rms,
            flat_peak_threshold,
            flat_std_threshold,
            noise_peak_to_rms_threshold,
        )
        channel_reports.append(
            NrdChannelStats(
                channel=channel,
                count=packets_analyzed,
                minimum=int(minimum[idx]),
                maximum=int(maximum[idx]),
                mean=float(mean[idx]),
                std=float(std[idx]),
                rms=channel_rms,
                mean_abs=float(mean_abs[idx]),
                max_abs=int(max_abs[idx]),
                peak_to_rms=peak_to_rms,
                quality=quality,
                electrode_type=_electrode_type_from_header(header_text, channel),
            )
        )

    estimated_duration = None
    if sample_rate_hz:
        estimated_duration = packets_analyzed / float(sample_rate_hz)

    return NrdStatsReport(
        path=str(layout.path),
        file_size=layout.file_size,
        header_bytes=layout.header_bytes,
        packet_size=layout.packet_size,
        n_channels=n_channels,
        packets_in_file=layout.packet_count,
        packets_analyzed=packets_analyzed,
        sample_rate_hz=sample_rate_hz,
        estimated_duration_seconds=estimated_duration,
        header_mentions_micro="micro" in header_lower,
        header_mentions_macro="macro" in header_lower,
        channels=channel_reports,
    )
