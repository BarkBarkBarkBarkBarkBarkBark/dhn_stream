"""
SpikeInterface ground-truth sender for the DHN dashboard.

Generates a finite synthetic electrophysiology recording (default 60 s),
then loops it indefinitely, transmitting as realtime headerless int16 UDP.
Pushes sender_stats to the dashboard WebSocket group at ~20 Hz.

The generation step takes a few seconds; a placeholder 'generating' status
is pushed immediately so the UI can show a spinner / feedback.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from darkhorse_neuralynx.udp_raw.raw_sender import RawUdpSender, scale_to_int16

_CHUNK_SECONDS = 0.05  # 50 ms per loop iteration → 20 Hz push rate


def run_si_sender(config: dict[str, Any], stop_event: threading.Event) -> None:
    """
    Background thread: generate SI recording → send as UDP → push stats to WS.

    config keys:
        dest_host           str     default "127.0.0.1"
        dest_port           int     default 26090
        channels            int     default 16
        sample_rate_hz      int     default 32000
        frames_per_packet   int     default 1
        target_peak         int     default 8000
        units               int     default 8   (SI neural units)
        seed                int     default 42
        duration_seconds    float   default 60.0 (recording length before looping)
        send_buffer         int     default 8 388 608
    """
    host       = config.get("dest_host", "127.0.0.1")
    port       = int(config.get("dest_port", 26090))
    n_channels = int(config.get("channels", 16))
    sr         = int(config.get("sample_rate_hz", 32000))
    fpp        = int(config.get("frames_per_packet", 1))
    target_pk  = int(config.get("target_peak", 8000))
    units      = int(config.get("units", 8))
    seed       = int(config.get("seed", 42))
    duration   = float(config.get("duration_seconds", 60.0))
    send_buf   = int(config.get("send_buffer", 8_388_608))

    channel_layer = get_channel_layer()
    chunk_frames  = max(1, int(sr * _CHUNK_SECONDS))

    def _push(kind: str, data: dict) -> None:
        async_to_sync(channel_layer.group_send)(
            "dashboard",
            {"type": "dashboard.update", "kind": kind, "data": data},
        )

    # Signal that we're alive but still generating (can take ~2-5 s for large configs)
    _push("sender_stats", {
        "running": True,
        "generating": True,
        "channels": n_channels,
        "sample_rate_hz": sr,
        "packet_rate": 0.0,
        "throughput_mbps": 0.0,
        "elapsed": 0.0,
        "packets_sent": 0,
        "bytes_sent": 0,
        "underruns": 0,
        "wave": [],
    })

    try:
        import spikeinterface.core as si
    except ImportError as exc:
        _push("sender_error", {"error": f"spikeinterface not installed: {exc}"})
        return

    recording, _ = si.generate_ground_truth_recording(
        durations=[duration],
        sampling_frequency=float(sr),
        num_channels=n_channels,
        num_units=units,
        seed=seed,
    )
    total_frames = recording.get_num_frames(segment_index=0)

    total_packets   = 0
    total_bytes     = 0
    total_underruns = 0
    t_start         = time.perf_counter()

    try:
        with RawUdpSender(host, port, send_buffer_bytes=send_buf) as sender:
            frame_start = 0
            while not stop_event.is_set():
                frame_end = min(frame_start + chunk_frames, total_frames)

                traces = recording.get_traces(
                    start_frame=frame_start,
                    end_frame=frame_end,
                    segment_index=0,
                    return_scaled=True,
                )
                traces_int16 = scale_to_int16(traces, target_peak=target_pk)

                stats = sender.send_chunk(
                    traces_int16,
                    sample_rate_hz=sr,
                    frames_per_packet=fpp,
                    layout="sample_major",
                )

                total_packets   += stats.packets_sent
                total_bytes     += stats.bytes_sent
                total_underruns += stats.underruns
                elapsed          = time.perf_counter() - t_start

                # Oscilloscope: last 50 ms decimated to 256 pts, normalised to [-1, 1]
                n_disp  = min(traces_int16.shape[1], 8)
                _WIN    = min(traces_int16.shape[0], int(sr * 0.05))
                _raw    = traces_int16[-_WIN:, :n_disp]
                _step   = max(1, _WIN // 256)
                _raw_d  = _raw[::_step][:256]
                _wf     = _raw_d.astype(np.float64)
                for _c in range(_wf.shape[1]):
                    _pk = float(np.max(np.abs(_wf[:, _c]))) or 1.0
                    _wf[:, _c] /= _pk
                _wave         = [_wf[:, c].tolist() for c in range(_wf.shape[1])]
                _ch_peaks_raw = [int(np.max(np.abs(_raw[:, c]))) for c in range(n_disp)]
                _ch_rms_raw   = [
                    round(float(np.sqrt(np.mean(_raw[:, c].astype(np.float64) ** 2))), 1)
                    for c in range(n_disp)
                ]

                _push("sender_stats", {
                    "packets_sent":      total_packets,
                    "bytes_sent":        total_bytes,
                    "underruns":         total_underruns,
                    "elapsed":           round(elapsed, 1),
                    "packet_rate":       round(stats.effective_packet_rate, 1),
                    "throughput_mbps":   round(stats.effective_throughput_mbps, 4),
                    "running":           True,
                    "channels":          n_channels,
                    "sample_rate_hz":    sr,
                    "frames_per_packet": fpp,
                    "target_peak":       target_pk,
                    "wave":              _wave,
                    "channel_peaks_raw": _ch_peaks_raw,
                    "channel_rms_raw":   _ch_rms_raw,
                    "loop_progress_pct": round(frame_start / total_frames * 100, 1),
                })

                frame_start = frame_end % total_frames  # seamless loop

    except Exception as exc:
        _push("sender_error", {"error": str(exc)})
    finally:
        _push("sender_stats", {"running": False})
