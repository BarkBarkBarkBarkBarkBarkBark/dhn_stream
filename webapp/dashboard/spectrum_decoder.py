"""
UDP spectrum decoder for the DHN dashboard.

Binds a UDP socket, accumulates samples into a pre-allocated numpy ring buffer
(zero-copy via memoryview), and pushes updates at two rates:
  • 20 Hz  — oscilloscope wave + latency (lightweight, ~3 KB per push)
  •  1 Hz  — full FFT spectrum + channel verification (heavier, ~15 KB per push)

The FFT operates on a full 1-second window for accurate frequency resolution.
The wave push shows the last 50 ms so 440 Hz appears as ~11 display pts per
period — clearly recognisable.

Verification logic:
  For each channel k (0-indexed), we expect harmonics 1 … (k+1) of the
  fundamental frequency. The decoder finds the top-(k+1) peaks in the
  FFT and checks that they're within `tolerance_hz` of the expected harmonics.
  A per-channel MATCH / MISMATCH flag is computed and sent with the spectrum.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import numpy as np
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

_TOLERANCE_HZ   = 10.0   # peak must be within this many Hz of expected harmonic
_WINDOW_SECONDS = 1.0    # FFT window length (1 s → 1 Hz resolution)
_FFT_INTERVAL   = 0.1    # push computed spectrum 10×/s (sliding window, same 1-s FFT)
_WAVE_INTERVAL  = 0.05   # oscilloscope push interval (20 Hz)


def _find_top_peaks(power_db: np.ndarray, freqs: np.ndarray, n: int) -> list[float]:
    """Return the frequencies of the n highest peaks in the power spectrum."""
    if n <= 0:
        return []
    indices = np.argsort(power_db)[::-1][:n]
    return sorted(freqs[indices].tolist())


def _check_harmonics(
    detected_hz: list[float],
    fundamental: float,
    n_harmonics: int,
    tolerance: float,
) -> bool:
    """Check that each expected harmonic (1..n_harmonics) has a detected peak nearby."""
    for h in range(1, n_harmonics + 1):
        expected = fundamental * h
        if not any(abs(d - expected) <= tolerance for d in detected_hz):
            return False
    return True


def run_spectrum_decoder(config: dict[str, Any], stop_event: threading.Event) -> None:
    """
    Background thread: receive UDP, FFT each channel, push spectrum to dashboard group.

    config keys:
        bind_host       str     default "0.0.0.0"
        receiver_port   int     default 26090
        channels        int     default 4
        sample_rate_hz  int     default 32000
        fundamental_hz  float   default 440.0
        frames_per_packet int   default 1
    """
    bind_host   = config.get("bind_host", "0.0.0.0")
    recv_port   = int(config.get("receiver_port", 26090))
    n_channels  = int(config.get("channels", 4))
    sr          = int(config.get("sample_rate_hz", 32000))
    fundamental = float(config.get("fundamental_hz", 440.0))
    fpp         = int(config.get("frames_per_packet", 1))

    channel_layer    = get_channel_layer()
    window_frames    = int(sr * _WINDOW_SECONDS)
    max_frames       = window_frames * 2            # 2-second ring
    bytes_per_frame  = n_channels * 2               # int16 LE
    bytes_per_packet = fpp * bytes_per_frame
    frames_per_push  = int(sr * _WAVE_INTERVAL)     # frames in 50 ms

    # ── Pre-allocated ring buffer ─────────────────────────────────────────────
    ring         = np.zeros((max_frames, n_channels), dtype="<i2")
    write_ptr    = 0
    frames_total = 0

    # ── Counters ──────────────────────────────────────────────────────────────
    packets_total = 0
    bytes_total   = 0
    packet_times: list[float] = []

    # ── Timers ────────────────────────────────────────────────────────────────
    _now        = time.perf_counter()
    t_last_wave = _now
    t_last_fft  = _now

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, recv_port))
    sock.settimeout(0.02)   # 20 ms → allows 20 Hz timer checks

    try:
        while not stop_event.is_set():
            # ── Receive ───────────────────────────────────────────────────────
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                data = None

            if data is not None and len(data) == bytes_per_packet:
                # Zero-copy parse via memoryview
                mv     = memoryview(data).cast("h")   # signed 16-bit view
                frames = np.frombuffer(mv, dtype="<i2", count=fpp * n_channels).reshape(fpp, n_channels)

                # Write into ring (handle boundary wrap)
                end = write_ptr + fpp
                if end <= max_frames:
                    ring[write_ptr:end] = frames
                else:
                    first = max_frames - write_ptr
                    ring[write_ptr:] = frames[:first]
                    ring[:fpp - first] = frames[first:]

                write_ptr    = end % max_frames
                frames_total += fpp
                packets_total += 1
                bytes_total   += len(data)
                packet_times.append(time.perf_counter())

            now = time.perf_counter()

            # ── 20 Hz wave push ───────────────────────────────────────────────
            if now - t_last_wave >= _WAVE_INTERVAL and frames_total >= frames_per_push:
                t_last_wave = now

                win_frames = min(frames_per_push, frames_total)
                start = (write_ptr - win_frames) % max_frames

                if start < write_ptr:
                    wave_window = ring[start:write_ptr]
                else:
                    wave_window = np.concatenate([ring[start:], ring[:write_ptr]], axis=0)

                _step = max(1, win_frames // 256)
                _raw  = wave_window[::_step, :min(n_channels, 8)][:256]  # int16 slice

                # Per-channel amplitude metrics in raw int16 units for diagnostic page
                _n_diag = _raw.shape[1]
                _ch_peaks_raw = [
                    int(np.max(np.abs(_raw[:, c].astype(np.int32))))
                    for c in range(_n_diag)
                ]
                _ch_rms_raw = [
                    round(float(np.sqrt(np.mean(_raw[:, c].astype(np.float64) ** 2))), 1)
                    for c in range(_n_diag)
                ]

                _w = _raw.astype(np.float32)
                for _c in range(_w.shape[1]):
                    _peak = float(np.max(np.abs(_w[:, _c]))) or 1.0
                    _w[:, _c] /= _peak
                _wave = [_w[:, _c].tolist() for _c in range(_w.shape[1])]

                _recent = packet_times[-200:]
                if len(_recent) >= 2:
                    _ivls = np.diff(_recent) * 1000.0
                    _lat = {
                        "mean_ms":       round(float(np.mean(_ivls)), 3),
                        "jitter_ms":     round(float(np.max(_ivls) - np.min(_ivls)), 3),
                        "since_last_ms": round((now - _recent[-1]) * 1000, 1),
                    }
                else:
                    _lat = {"mean_ms": None, "jitter_ms": None, "since_last_ms": None}

                async_to_sync(channel_layer.group_send)(
                    "dashboard",
                    {
                        "type": "dashboard.update",
                        "kind": "spectrum",
                        "data": {
                            "running":          True,
                            "wave":             _wave,
                            "latency":          _lat,
                            "packets_total":    packets_total,
                            "bytes_total":      bytes_total,
                            "channel_peaks_raw": _ch_peaks_raw,
                            "channel_rms_raw":   _ch_rms_raw,
                            "config": {
                                "sample_rate_hz": sr,
                                "frames_per_packet": fpp,
                                "fundamental_hz": fundamental,
                                "bind_host": bind_host,
                                "recv_port": recv_port,
                            },
                        },
                    },
                )

                if len(packet_times) > 4000:
                    packet_times[:] = packet_times[-2000:]

            # ── 10 Hz FFT push (sliding 1-second window) ─────────────────────
            if now - t_last_fft >= _FFT_INTERVAL and frames_total >= window_frames:
                t_last_fft = now

                start = (write_ptr - window_frames) % max_frames
                if start < write_ptr:
                    window = ring[start:write_ptr].astype(np.float64)
                else:
                    window = np.concatenate([ring[start:], ring[:write_ptr]], axis=0).astype(np.float64)

                # Vectorised FFT across all channels at once
                fft_all   = np.fft.rfft(window, axis=0)                   # (bins, n_ch)
                mag_all   = np.abs(fft_all) / window_frames
                db_all    = 20.0 * np.log10(np.maximum(mag_all, 1e-6))    # (bins, n_ch)
                freqs_arr = np.fft.rfftfreq(window_frames, d=1.0 / sr)

                channel_spectra: list[list[float]] = []
                channel_matches: list[bool]        = []
                all_expected:    list[list[float]] = []

                for ch in range(n_channels):
                    power_db = db_all[:, ch]
                    channel_spectra.append(power_db.tolist())

                    n_harmonics = ch + 1 if ch < 4 else 0
                    expected    = [fundamental * h for h in range(1, n_harmonics + 1)]
                    all_expected.append(expected)

                    top_peaks = (
                        _find_top_peaks(power_db, freqs_arr, n_harmonics)
                        if n_harmonics else []
                    )
                    channel_matches.append(
                        _check_harmonics(top_peaks, fundamental, n_harmonics, _TOLERANCE_HZ)
                    )

                _max_hz  = min(sr / 2.0, fundamental * 8)
                _max_bin = min(len(freqs_arr), int(_max_hz * window_frames / sr) + 2)
                _ds      = max(1, _max_bin // 512)
                display_freqs   = freqs_arr[:_max_bin:_ds][:512].tolist()
                display_spectra = [ch[:_max_bin:_ds][:512] for ch in channel_spectra]

                async_to_sync(channel_layer.group_send)(
                    "dashboard",
                    {
                        "type": "dashboard.update",
                        "kind": "spectrum",
                        "data": {
                            "running":            True,
                            "freqs":              display_freqs,
                            "channel_spectra":    display_spectra,
                            "channel_matches":    channel_matches,
                            "expected_harmonics": all_expected,
                            "fundamental_hz":     fundamental,
                            "packets_total":      packets_total,
                            "bytes_total":        bytes_total,
                        },
                    },
                )

    except Exception as exc:
        async_to_sync(channel_layer.group_send)(
            "dashboard",
            {
                "type": "dashboard.update",
                "kind": "receiver_error",
                "data": {"error": str(exc)},
            },
        )
    finally:
        sock.close()
        async_to_sync(channel_layer.group_send)(
            "dashboard",
            {
                "type": "dashboard.update",
                "kind": "spectrum",
                "data": {"running": False},
            },
        )
