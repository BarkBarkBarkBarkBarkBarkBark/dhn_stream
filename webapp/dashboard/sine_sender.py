"""
Sine wave UDP sender for the DHN dashboard.

Channel harmonic design:
  Ch 0: fundamental only                          (1 component)
  Ch 1: fundamental + 2nd harmonic               (2 components)
  Ch 2: fundamental + 2nd + 3rd harmonic         (3 components)
  Ch 3: fundamental + 2nd + 3rd + 4th harmonic   (4 components)

Each channel's power spectrum is therefore a unique fingerprint — the receiver
can trivially verify that exactly the right harmonics appear on each channel.

Additional channels (4+) receive no signal (zeros) so they don't confuse the
spectrum decoder.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from typing import Any

import numpy as np
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from darkhorse_neuralynx.udp_raw.raw_sender import RawUdpSender, SendStats, scale_to_int16


# How many samples to synthesize per loop iteration (50ms → 20 Hz push rate)
_CHUNK_SECONDS = 0.05


def _build_harmonic_chunk(
    n_frames: int,
    n_channels: int,
    sample_rate_hz: int,
    fundamental_hz: float,
    amplitude: float = 0.8,
    t_offset: float = 0.0,
) -> np.ndarray:
    """
    Build one chunk of multi-channel harmonic sine data.

    Ch k (0-indexed) contains harmonics 1 … (k+1) of the fundamental.
    Channels beyond 3 are zeros.

    Returns float64 array shaped (n_frames, n_channels) with values in [-1, 1].
    """
    t = np.linspace(t_offset, t_offset + n_frames / sample_rate_hz, n_frames, endpoint=False)
    out = np.zeros((n_frames, n_channels), dtype=np.float64)

    for ch in range(min(n_channels, 4)):
        n_harmonics = ch + 1  # ch 0 → 1 harmonic, ch 3 → 4 harmonics
        sig = np.zeros(n_frames, dtype=np.float64)
        for h in range(1, n_harmonics + 1):
            sig += np.sin(2 * math.pi * fundamental_hz * h * t)
        # Normalise so the peak of the sum is ≤ amplitude
        peak = np.max(np.abs(sig)) or 1.0
        out[:, ch] = sig * (amplitude / peak)

    return out


def run_sine_sender(config: dict[str, Any], stop_event: threading.Event) -> None:
    """
    Background thread: generate harmonic sine waves and stream via UDP.

    config keys:
        dest_host       str     default "127.0.0.1"
        dest_port       int     default 26090
        channels        int     default 4
        sample_rate_hz  int     default 32000
        fundamental_hz  float   default 440.0
        frames_per_packet int   default 1
        target_peak     int     default 8000
        send_buffer     int     default 8388608
    """
    host = config.get("dest_host", "127.0.0.1")
    port = int(config.get("dest_port", 26090))
    n_channels = int(config.get("channels", 4))
    sr = int(config.get("sample_rate_hz", 32000))
    fundamental = float(config.get("fundamental_hz", 440.0))
    fpp = int(config.get("frames_per_packet", 1))
    target_peak = int(config.get("target_peak", 8000))
    send_buffer = int(config.get("send_buffer", 8_388_608))

    channel_layer = get_channel_layer()
    chunk_frames = max(1, int(sr * _CHUNK_SECONDS))  # 50 ms = 1600 frames @ 32 kHz

    total_packets = 0
    total_bytes = 0
    total_underruns = 0
    t_start = time.perf_counter()
    t_offset = 0.0

    try:
        with RawUdpSender(host, port, send_buffer_bytes=send_buffer) as sender:
            while not stop_event.is_set():
                chunk = _build_harmonic_chunk(
                    chunk_frames, n_channels, sr, fundamental, t_offset=t_offset
                )
                chunk_int16 = scale_to_int16(chunk, target_peak=target_peak)

                stats: SendStats = sender.send_chunk(
                    chunk_int16,
                    sample_rate_hz=sr,
                    frames_per_packet=fpp,
                    layout="sample_major",
                )

                total_packets += stats.packets_sent
                total_bytes += stats.bytes_sent
                total_underruns += stats.underruns
                t_offset += chunk_frames / sr
                elapsed = time.perf_counter() - t_start

                # Oscilloscope: show last 50 ms at 32 kHz → 1600 samples, decimate by 6 → 256 pts
                # This guarantees 440 Hz shows ~11 display pts per period (clearly visible)
                _WIN = min(chunk_frames, int(sr * 0.05))  # last 50 ms
                _wave_src = chunk[-_WIN:, :min(n_channels, 8)]  # float64 in [-1, 1]
                _step = max(1, _WIN // 256)
                _wave_arr = _wave_src[::_step][:256]
                _wave = [_wave_arr[:, c].tolist() for c in range(_wave_arr.shape[1])]

                # Per-channel amplitude metrics (in int16 units) for diagnostic page
                _n_diag = _wave_arr.shape[1]
                _ch_peaks_raw = [
                    int(round(float(np.max(np.abs(_wave_arr[:, c]))) * target_peak))
                    for c in range(_n_diag)
                ]
                _ch_rms_raw = [
                    round(float(np.sqrt(np.mean(_wave_arr[:, c] ** 2))) * target_peak, 1)
                    for c in range(_n_diag)
                ]

                payload = {
                    "type": "dashboard.update",
                    "kind": "sender_stats",
                    "data": {
                        "packets_sent": total_packets,
                        "bytes_sent": total_bytes,
                        "underruns": total_underruns,
                        "elapsed": round(elapsed, 1),
                        "packet_rate": round(stats.effective_packet_rate, 1),
                        "throughput_mbps": round(stats.effective_throughput_mbps, 4),
                        "running": True,
                        "fundamental_hz": fundamental,
                        "channels": n_channels,
                        "sample_rate_hz": sr,
                        "frames_per_packet": fpp,
                        "target_peak": target_peak,
                        "wave": _wave,
                        "channel_peaks_raw": _ch_peaks_raw,
                        "channel_rms_raw": _ch_rms_raw,
                    },
                }
                async_to_sync(channel_layer.group_send)("dashboard", payload)

    except Exception as exc:
        async_to_sync(channel_layer.group_send)(
            "dashboard",
            {
                "type": "dashboard.update",
                "kind": "sender_error",
                "data": {"error": str(exc)},
            },
        )
    finally:
        async_to_sync(channel_layer.group_send)(
            "dashboard",
            {
                "type": "dashboard.update",
                "kind": "sender_stats",
                "data": {"running": False},
            },
        )
