"""Live NRD UDP monitor state for the optional dashboard."""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from darkhorse_neuralynx.dashboard.labels import ChannelLabel, merge_labels
from darkhorse_neuralynx.udp_raw.nrd_sender import parse_nrd_packet


@dataclass(frozen=True)
class DownsampledWaveform:
    channel: int
    samples_seen: int
    min_values: list[int]
    max_values: list[int]


def min_max_downsample(samples: np.ndarray, target_bins: int) -> tuple[np.ndarray, np.ndarray]:
    """Return per-bin min and max arrays, preserving spikes better than slicing."""
    values = np.asarray(samples, dtype=np.int32)
    if values.size == 0:
        return np.array([], dtype=np.int32), np.array([], dtype=np.int32)
    bins = max(1, min(int(target_bins), values.size))
    edges = np.linspace(0, values.size, bins + 1, dtype=np.int64)
    minima = np.empty(bins, dtype=np.int32)
    maxima = np.empty(bins, dtype=np.int32)
    for idx in range(bins):
        segment = values[edges[idx] : edges[idx + 1]]
        if segment.size == 0:
            segment = values[-1:]
        minima[idx] = int(segment.min())
        maxima[idx] = int(segment.max())
    return minima, maxima


class LiveNrdMonitor:
    """Threaded UDP listener plus rolling per-channel metrics for NRD packets."""

    def __init__(
        self,
        *,
        expected_channels: int | None = None,
        sample_rate_hz: int = 32_000,
        waveform_seconds: float = 1.0,
        labels: list[ChannelLabel] | None = None,
        flat_peak_threshold: int = 0,
        flat_std_threshold: float = 1.0,
        noise_peak_to_rms_threshold: float = 8.0,
    ) -> None:
        self.expected_channels = expected_channels
        self.sample_rate_hz = sample_rate_hz
        self.waveform_seconds = waveform_seconds
        self.flat_peak_threshold = flat_peak_threshold
        self.flat_std_threshold = flat_std_threshold
        self.noise_peak_to_rms_threshold = noise_peak_to_rms_threshold
        self._labels = labels or []
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._reset_state(expected_channels or 0)

    def start_udp_listener(self, host: str, port: int) -> None:
        """Start a background UDP listener."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_udp, args=(host, port), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def update_packet(self, packet: bytes, *, received_at: float | None = None) -> None:
        """Parse one NRD UDP packet and update packet/channel metrics."""
        now = time.time() if received_at is None else received_at
        try:
            decoded = parse_nrd_packet(packet)
        except ValueError:
            with self._lock:
                self.parse_errors += 1
                self.last_packet_at = now
            return

        samples = np.asarray(decoded["samples"], dtype=np.int32)
        packet_id = int(decoded["packet_id"])
        timestamp_us = int(decoded["timestamp_us"])
        with self._lock:
            if self.n_channels == 0:
                self._reset_state(int(samples.size))
            if samples.size != self.n_channels:
                self.parse_errors += 1
                self.last_packet_at = now
                return
            if self.expected_channels is not None and samples.size != self.expected_channels:
                self.channel_mismatch_errors += 1
            if self.last_packet_id is not None:
                expected_id = (self.last_packet_id + 1) & 0xFFFFFFFF
                if packet_id != expected_id:
                    self.packet_id_gaps += 1
            if self.last_timestamp_us is not None and timestamp_us <= self.last_timestamp_us:
                self.timestamp_gaps += 1

            self.last_packet_id = packet_id
            self.last_timestamp_us = timestamp_us
            self.packet_count += 1
            self.byte_count += len(packet)
            self.last_packet_size = len(packet)
            self.last_packet_at = now
            self.packet_times.append(now)
            self._trim_packet_times(now)
            self._update_channel_stats(samples)

    def snapshot(self, *, max_channels: int | None = None) -> dict[str, Any]:
        """Return JSON-serializable monitor state."""
        now = time.time()
        with self._lock:
            self._trim_packet_times(now)
            rate_window_seconds = 0.0
            if len(self.packet_times) >= 2:
                rate_window_seconds = max(self.packet_times[-1] - self.packet_times[0], 1e-9)
            packet_rate = (len(self.packet_times) - 1) / rate_window_seconds if rate_window_seconds else 0.0
            byte_rate = (self.last_packet_size * packet_rate) if self.last_packet_size else 0.0
            labels = self._labels or merge_labels(self.n_channels)
            channels = []
            row_limit = self.n_channels if max_channels is None else min(max_channels, self.n_channels)
            for idx in range(row_limit):
                stats = self._channel_snapshot(idx)
                label = labels[idx] if idx < len(labels) else ChannelLabel(idx + 1, f"ch{idx + 1:04d}")
                stats.update({"label": label.to_dict()})
                channels.append(stats)
            return {
                "n_channels": self.n_channels,
                "sample_rate_hz": self.sample_rate_hz,
                "packet_count": self.packet_count,
                "byte_count": self.byte_count,
                "last_packet_size": self.last_packet_size,
                "packet_rate_hz": packet_rate,
                "throughput_mbps": (byte_rate * 8.0) / 1_000_000.0,
                "parse_errors": self.parse_errors,
                "channel_mismatch_errors": self.channel_mismatch_errors,
                "packet_id_gaps": self.packet_id_gaps,
                "timestamp_gaps": self.timestamp_gaps,
                "last_packet_age_seconds": (now - self.last_packet_at) if self.last_packet_at else None,
                "channels": channels,
            }

    def waveform(self, channel: int, *, bins: int = 600) -> DownsampledWaveform:
        """Return min/max decimated waveform data for a 1-based channel."""
        with self._lock:
            if channel < 1 or channel > self.n_channels or self.waveforms is None:
                return DownsampledWaveform(channel, 0, [], [])
            idx = channel - 1
            samples_seen = min(self.samples_seen, self.waveform_size)
            if samples_seen <= 0:
                return DownsampledWaveform(channel, 0, [], [])
            start = (self.waveform_pos - samples_seen) % self.waveform_size
            if start < self.waveform_pos:
                values = self.waveforms[idx, start:self.waveform_pos].copy()
            else:
                values = np.concatenate((self.waveforms[idx, start:], self.waveforms[idx, : self.waveform_pos]))
        minima, maxima = min_max_downsample(values, bins)
        return DownsampledWaveform(
            channel=channel,
            samples_seen=int(samples_seen),
            min_values=[int(value) for value in minima],
            max_values=[int(value) for value in maxima],
        )

    def _reset_state(self, n_channels: int) -> None:
        self.n_channels = n_channels
        self.packet_count = 0
        self.byte_count = 0
        self.last_packet_size = 0
        self.parse_errors = 0
        self.channel_mismatch_errors = 0
        self.packet_id_gaps = 0
        self.timestamp_gaps = 0
        self.last_packet_id: int | None = None
        self.last_timestamp_us: int | None = None
        self.last_packet_at: float | None = None
        self.packet_times: deque[float] = deque()
        self.samples_seen = 0
        self.waveform_pos = 0
        self.waveform_size = max(1, int(self.sample_rate_hz * self.waveform_seconds))
        if n_channels > 0:
            self.minimum = np.full(n_channels, np.iinfo(np.int32).max, dtype=np.int64)
            self.maximum = np.full(n_channels, np.iinfo(np.int32).min, dtype=np.int64)
            self.sum_values = np.zeros(n_channels, dtype=np.float64)
            self.sum_squares = np.zeros(n_channels, dtype=np.float64)
            self.sum_abs = np.zeros(n_channels, dtype=np.float64)
            self.waveforms = np.zeros((n_channels, self.waveform_size), dtype=np.int32)
        else:
            self.minimum = np.array([], dtype=np.int64)
            self.maximum = np.array([], dtype=np.int64)
            self.sum_values = np.array([], dtype=np.float64)
            self.sum_squares = np.array([], dtype=np.float64)
            self.sum_abs = np.array([], dtype=np.float64)
            self.waveforms = None

    def _listen_udp(self, host: str, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.bind((host, port))
        self._sock = sock
        try:
            while not self._stop_event.is_set():
                try:
                    packet, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self.update_packet(packet)
        finally:
            self._sock = None
            sock.close()

    def _trim_packet_times(self, now: float) -> None:
        cutoff = now - 2.0
        while self.packet_times and self.packet_times[0] < cutoff:
            self.packet_times.popleft()

    def _update_channel_stats(self, samples: np.ndarray) -> None:
        samples_i64 = samples.astype(np.int64, copy=False)
        self.minimum = np.minimum(self.minimum, samples_i64)
        self.maximum = np.maximum(self.maximum, samples_i64)
        self.sum_values += samples_i64
        self.sum_squares += np.square(samples_i64, dtype=np.float64)
        self.sum_abs += np.abs(samples_i64)
        if self.waveforms is not None:
            self.waveforms[:, self.waveform_pos] = samples
            self.waveform_pos = (self.waveform_pos + 1) % self.waveform_size
        self.samples_seen += 1

    def _channel_snapshot(self, idx: int) -> dict[str, Any]:
        if self.samples_seen <= 0:
            return {
                "channel": idx + 1,
                "count": 0,
                "minimum": None,
                "maximum": None,
                "mean": None,
                "std": None,
                "rms": None,
                "mean_abs": None,
                "max_abs": None,
                "peak_to_rms": None,
                "quality": "no-data",
            }
        count = float(self.samples_seen)
        mean = float(self.sum_values[idx] / count)
        variance = max(float(self.sum_squares[idx] / count) - (mean * mean), 0.0)
        std = float(np.sqrt(variance))
        rms = float(np.sqrt(float(self.sum_squares[idx] / count)))
        mean_abs = float(self.sum_abs[idx] / count)
        max_abs = int(max(abs(int(self.minimum[idx])), abs(int(self.maximum[idx]))))
        peak_to_rms = float(max_abs / rms) if rms > 0 else 0.0
        quality = self._quality_label(max_abs, std, peak_to_rms)
        return {
            "channel": idx + 1,
            "count": self.samples_seen,
            "minimum": int(self.minimum[idx]),
            "maximum": int(self.maximum[idx]),
            "mean": mean,
            "std": std,
            "rms": rms,
            "mean_abs": mean_abs,
            "max_abs": max_abs,
            "peak_to_rms": peak_to_rms,
            "quality": quality,
        }

    def _quality_label(self, max_abs: int, std: float, peak_to_rms: float) -> str:
        if max_abs <= self.flat_peak_threshold or std <= self.flat_std_threshold:
            return "flat"
        if peak_to_rms <= self.noise_peak_to_rms_threshold:
            return "noise-like"
        return "signal-like"
