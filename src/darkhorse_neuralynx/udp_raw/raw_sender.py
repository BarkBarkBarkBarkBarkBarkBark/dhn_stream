"""
Raw UDP sender: serialize numpy trace chunks and send headerless UDP packets.

Payload contract:
  - dtype: int16, little-endian
  - layout: sample_major by default (frame0_ch0, frame0_ch1, ... frame1_ch0, ...)
  - no application header bytes
  - paced to match realtime sample rate using time.perf_counter

Usage example:
    with RawUdpSender("192.168.3.50", 26090) as sender:
        stats = sender.send_chunk(traces, sample_rate_hz=32000, frames_per_packet=1)
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

import numpy as np

UDP_WARN_BYTES = 1400  # Ethernet MTU safety threshold for unfragmented UDP payload


@dataclass
class SendStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    underruns: int = 0
    elapsed_seconds: float = 0.0

    @property
    def effective_packet_rate(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.packets_sent / self.elapsed_seconds

    @property
    def effective_throughput_mbps(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return (self.bytes_sent * 8) / (self.elapsed_seconds * 1_000_000)


class RawUdpSender:
    """Context manager that owns a UDP socket and sends headerless int16 payloads."""

    def __init__(
        self,
        host: str,
        port: int,
        send_buffer_bytes: int = 8_388_608,
        broadcast: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.send_buffer_bytes = send_buffer_bytes
        self.broadcast = broadcast
        self._sock: socket.socket | None = None

    def __enter__(self) -> "RawUdpSender":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.send_buffer_bytes)
        if self.broadcast:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return self

    def __exit__(self, *_: object) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def send_chunk(
        self,
        traces: np.ndarray,
        sample_rate_hz: int,
        frames_per_packet: int = 1,
        layout: str = "sample_major",
    ) -> SendStats:
        """
        Send `traces` shaped (n_frames, n_channels) as a stream of UDP packets.

        Args:
            traces: 2D array shaped (n_frames, n_channels). Values must already
                    be in int16 range — use scale_to_int16() before calling.
            sample_rate_hz: Sample rate for real-time pacing.
            frames_per_packet: How many frames to pack into one UDP datagram.
            layout: "sample_major" (default) or "channel_major".

        Returns:
            SendStats with packet counts, byte counts, underruns, and elapsed time.
        """
        if self._sock is None:
            raise RuntimeError("RawUdpSender must be used as a context manager.")

        traces = np.asarray(traces, dtype="<i2")  # int16, little-endian

        if layout == "channel_major":
            # Transpose so channels are the fast axis: (n_channels, n_frames)
            traces = np.ascontiguousarray(traces.T)
        else:
            traces = np.ascontiguousarray(traces)  # sample_major: (n_frames, n_channels)

        n_frames, n_channels = traces.shape if layout == "sample_major" else (traces.shape[1], traces.shape[0])

        payload_bytes = frames_per_packet * n_channels * 2  # 2 bytes per int16
        if payload_bytes > UDP_WARN_BYTES:
            print(
                f"WARNING: payload {payload_bytes} bytes exceeds {UDP_WARN_BYTES}-byte threshold. "
                "Packet may be fragmented unless jumbo frames are configured."
            )

        stats = SendStats()
        period_s = frames_per_packet / sample_rate_hz
        dest = (self.host, self.port)

        # Flatten into (n_frames, n_channels) for iteration regardless of layout
        if layout == "sample_major":
            flat = traces  # (n_frames, n_channels)
            total_frames = flat.shape[0]
        else:
            flat = traces.T  # back to (n_frames, n_channels) for iteration
            total_frames = flat.shape[0]

        t_start = time.perf_counter()
        packet_index = 0
        frame_index = 0

        while frame_index + frames_per_packet <= total_frames:
            chunk = flat[frame_index : frame_index + frames_per_packet]

            if layout == "channel_major":
                # Re-transpose chunk to channel_major for wire format
                payload = np.ascontiguousarray(chunk.T).tobytes()
            else:
                payload = chunk.tobytes(order="C")

            t_deadline = t_start + packet_index * period_s
            now = time.perf_counter()
            if now < t_deadline:
                time.sleep(t_deadline - now)
            else:
                if packet_index > 0:
                    stats.underruns += 1

            self._sock.sendto(payload, dest)
            stats.packets_sent += 1
            stats.bytes_sent += len(payload)

            frame_index += frames_per_packet
            packet_index += 1

        stats.elapsed_seconds = time.perf_counter() - t_start
        return stats


def scale_to_int16(traces: np.ndarray, target_peak: int = 8000) -> np.ndarray:
    """
    Scale a float trace array to int16 with a given target peak absolute value.

    Clips values that would overflow int16 range [-32768, 32767].
    Never silently changes dtype without returning the new array.

    Args:
        traces: Float array of any shape.
        target_peak: Desired peak absolute value in the int16 output (default 8000).

    Returns:
        int16 numpy array, same shape as input.
    """
    traces = np.asarray(traces, dtype=np.float64)
    peak = np.max(np.abs(traces))
    if peak > 0:
        scaled = traces * (target_peak / peak)
    else:
        scaled = traces.copy()
    clipped = np.clip(scaled, -32768, 32767)
    return clipped.astype(np.int16)
