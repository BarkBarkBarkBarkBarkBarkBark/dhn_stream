"""
NRD-format UDP sender — true Neuralynx wire format for DHN_Acq.

Per Matt @ NLX (Apr 2026):
    "The data needs to be sent in NLX packets. The NRD file is a series of
     packets, each is a separate transmission on the UDP connection. The
     size depends on the number of channels: Header bytes + (4 * number of
     channels) + (4 trailing CRC bytes). DHN will figure out the sampling
     rate based on the packet rate."

Packet layout (little-endian, one packet PER SAMPLE TIMESTEP carrying ALL
channels as int32):

    Offset  Size  Type        Field
    ------  ----  ----------  ---------------------------------------
         0     4  uint32      stx                  (always 0x00000800)
         4     4  int32       packet_id            (monotonic, +1 each)
         8     4  int32       size_int32_words     (n_channels + 10)
        12     4  uint32      timestamp_high       (µs, upper 32 bits)
        16     4  uint32      timestamp_low        (µs, lower 32 bits)
        20     4  int32       status
        24     4  uint32      parallel_port        (TTL bits)
        28    40  int32[10]   extras               (reserved / board status)
        68    4*N int32       samples[n_channels]
      68+4N    4  uint32      crc                  (XOR of all preceding int32)

Total packet size: 68 + 4 * n_channels + 4 bytes  (= 72 + 4*N).

NOTE: The header layout above is the widely-documented NLX Cheetah/Pegasus
NRD format. Field offsets and the CRC algorithm (XOR-of-int32) MUST be
confirmed against a real Pegasus-replay pcap and/or the NLX formatting
document before treating this as canonical. See `docs/smoke_tests.yaml`
`open_questions` and `docs/nrd-format.md` for the verification checklist.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

import numpy as np

# --- NRD wire-format constants -----------------------------------------------
NRD_STX: int = 0x00000800
# Header = stx, pkt_id, size, ts_hi, ts_lo, status, parport, extras[10]
#       =  1 +  1    +  1  +  1   +  1   +  1    +  1     + 10 = 17 int32 words.
NRD_HEADER_INT32_WORDS: int = 17
NRD_HEADER_SIZE_BYTES: int = NRD_HEADER_INT32_WORDS * 4   # 68
NRD_EXTRAS_COUNT: int = 10
NRD_CRC_SIZE_BYTES: int = 4
# size_int32_words convention: count of int32 words AFTER the (stx, pkt_id,
# size) preamble and BEFORE the CRC. Static part = ts_hi, ts_lo, status,
# parport, extras[10] = 14 words; n_channels samples are added at runtime.
_STATUS_WORDS_AFTER_SIZE: int = 14


def nrd_packet_size_bytes(n_channels: int) -> int:
    """Return the on-wire size of a single NRD UDP packet for `n_channels`."""
    return NRD_HEADER_SIZE_BYTES + 4 * n_channels + NRD_CRC_SIZE_BYTES


def _xor_crc_int32(buf: bytes) -> int:
    """XOR all int32 words in `buf` (little-endian). Length must be a multiple of 4."""
    arr = np.frombuffer(buf, dtype="<u4")
    crc = np.bitwise_xor.reduce(arr) if arr.size else np.uint32(0)
    return int(np.uint32(crc))


def build_nrd_packet(
    packet_id: int,
    timestamp_us: int,
    samples_int32: np.ndarray,
    *,
    status: int = 0,
    parallel_port: int = 0,
    extras: np.ndarray | None = None,
) -> bytes:
    """
    Build one NRD UDP packet covering all channels at a single timestep.

    Args:
        packet_id:      monotonic per-stream packet counter (int32).
        timestamp_us:   absolute timestamp in microseconds (uint64).
        samples_int32:  shape (n_channels,), dtype convertible to int32.
        status:         board status word (int32). Default 0.
        parallel_port:  TTL bits (uint32). Default 0.
        extras:         optional shape (10,) int32 array. Defaults to zeros.

    Returns:
        bytes object of length `nrd_packet_size_bytes(n_channels)`.
    """
    samples = np.ascontiguousarray(samples_int32, dtype="<i4")
    n_channels = samples.size
    if extras is None:
        extras_arr = np.zeros(NRD_EXTRAS_COUNT, dtype="<i4")
    else:
        extras_arr = np.ascontiguousarray(extras, dtype="<i4")
        if extras_arr.size != NRD_EXTRAS_COUNT:
            raise ValueError(f"extras must have {NRD_EXTRAS_COUNT} elements")

    size_word = _STATUS_WORDS_AFTER_SIZE + n_channels
    ts = int(timestamp_us) & 0xFFFFFFFFFFFFFFFF
    ts_hi = (ts >> 32) & 0xFFFFFFFF
    ts_lo = ts & 0xFFFFFFFF

    # Pack 17-word header: stx, pkt_id, size, ts_hi, ts_lo, status, parport, extras[10]
    header = struct.pack(
        "<IiiIIiI",
        NRD_STX,
        int(packet_id) & 0xFFFFFFFF,
        size_word,
        ts_hi,
        ts_lo,
        int(status),
        int(parallel_port) & 0xFFFFFFFF,
    )
    header += extras_arr.tobytes()
    body = header + samples.tobytes()
    crc = _xor_crc_int32(body)
    return body + struct.pack("<I", crc)


def parse_nrd_packet(pkt: bytes) -> dict:
    """
    Inverse of `build_nrd_packet`. Returns a dict of decoded fields.
    Raises ValueError on size or STX or CRC mismatch.
    """
    if len(pkt) < NRD_HEADER_SIZE_BYTES + NRD_CRC_SIZE_BYTES:
        raise ValueError(f"packet too short: {len(pkt)} bytes")
    if (len(pkt) - NRD_HEADER_SIZE_BYTES - NRD_CRC_SIZE_BYTES) % 4 != 0:
        raise ValueError(f"payload not int32-aligned: {len(pkt)} bytes")

    stx, pkt_id, size_word, ts_hi, ts_lo, status, parport = struct.unpack(
        "<IiiIIiI", pkt[:28]
    )
    if stx != NRD_STX:
        raise ValueError(f"bad STX: 0x{stx:08x}")

    extras = np.frombuffer(pkt[28:68], dtype="<i4").copy()
    body_end = len(pkt) - NRD_CRC_SIZE_BYTES
    samples = np.frombuffer(pkt[68:body_end], dtype="<i4").copy()
    crc_actual = int.from_bytes(pkt[body_end:], "little")
    crc_expected = _xor_crc_int32(pkt[:body_end])
    if crc_actual != crc_expected:
        raise ValueError(f"CRC mismatch: got 0x{crc_actual:08x}, want 0x{crc_expected:08x}")

    return {
        "stx": stx,
        "packet_id": pkt_id,
        "size_int32_words": size_word,
        "timestamp_us": (ts_hi << 32) | ts_lo,
        "status": status,
        "parallel_port": parport,
        "extras": extras,
        "samples": samples,
        "n_channels": samples.size,
        "crc": crc_actual,
    }


# --- Sender ------------------------------------------------------------------

@dataclass
class NrdSendStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    underruns: int = 0
    mirror_packets_sent: int = 0
    mirror_bytes_sent: int = 0
    mirror_underruns: int = 0
    elapsed_seconds: float = 0.0

    @property
    def effective_packet_rate(self) -> float:
        return self.packets_sent / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def effective_throughput_mbps(self) -> float:
        return (self.bytes_sent * 8) / (self.elapsed_seconds * 1_000_000) if self.elapsed_seconds > 0 else 0.0


class NrdUdpSender:
    """
    UDP sender that emits true Neuralynx NRD packets.

    One UDP datagram per sample timestep, carrying all channels as int32.
    Paced to realtime so DHN_Acq can infer the sample rate from packet rate.
    """

    def __init__(
        self,
        host: str,
        port: int,
        send_buffer_bytes: int = 8_388_608,
        broadcast: bool = False,
        mirror_targets: tuple[tuple[str, int], ...] = (),
    ) -> None:
        self.host = host
        self.port = port
        self.send_buffer_bytes = send_buffer_bytes
        self.broadcast = broadcast
        self.mirror_targets = mirror_targets
        self._sock: socket.socket | None = None
        self._packet_id: int = 0
        self._timestamp_us: int = 0
        self._ts_accum_us: float = 0.0
        self._next_send: float = 0.0

    def __enter__(self) -> "NrdUdpSender":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.send_buffer_bytes)
        except OSError:
            pass
        self._sock = s
        self.reset_pacing()
        return self

    def reset_pacing(self) -> None:
        """Start realtime pacing from now without changing packet IDs/timestamps."""
        self._next_send = time.perf_counter()

    def __exit__(self, *_: object) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def send_chunk(
        self,
        chunk: np.ndarray,
        sample_rate_hz: int,
    ) -> NrdSendStats:
        """
        Send a chunk of samples as one NRD packet per timestep (frame).

        Args:
            chunk: shape (n_frames, n_channels), numeric (cast to int32).
                   int16 inputs are widened automatically; for amplitudes that
                   need the full int32 ADC range, scale upstream.
            sample_rate_hz: realtime pacing rate; one packet sent every
                            (1 / sample_rate_hz) seconds.

        Returns:
            NrdSendStats for this chunk.
        """
        assert self._sock is not None, "Use as context manager"
        chunk_i32 = np.ascontiguousarray(chunk, dtype="<i4")
        n_frames, _n_channels = chunk_i32.shape
        period_s = 1.0 / float(sample_rate_hz)
        # Fractional us/sample accumulator: no rate drift even at 32 kHz.
        # (1e6/32000 = 31.25 us; rounding to int 31 yields 32258 Hz which DHN_Acq
        # will report. We track the exact fractional remainder per packet.)
        ts_step_us_float = 1_000_000.0 / float(sample_rate_hz)

        stats = NrdSendStats()
        t0 = time.perf_counter()

        for frame_idx in range(n_frames):
            # Pace to realtime, one packet per timestep
            now = time.perf_counter()
            if now < self._next_send:
                time.sleep(self._next_send - now)
            self._next_send += period_s

            pkt = build_nrd_packet(
                packet_id=self._packet_id,
                timestamp_us=self._timestamp_us,
                samples_int32=chunk_i32[frame_idx],
            )
            try:
                self._sock.sendto(pkt, (self.host, self.port))
                stats.packets_sent += 1
                stats.bytes_sent += len(pkt)
            except OSError:
                stats.underruns += 1

            for mirror_host, mirror_port in self.mirror_targets:
                try:
                    self._sock.sendto(pkt, (mirror_host, mirror_port))
                    stats.mirror_packets_sent += 1
                    stats.mirror_bytes_sent += len(pkt)
                except OSError:
                    stats.mirror_underruns += 1

            self._packet_id = (self._packet_id + 1) & 0xFFFFFFFF
            # Carry fractional remainder so the integer µs timestamp tracks
            # the true sample rate over long runs.
            self._ts_accum_us += ts_step_us_float
            ts_increment = int(self._ts_accum_us)
            self._ts_accum_us -= ts_increment
            self._timestamp_us += ts_increment

        stats.elapsed_seconds = time.perf_counter() - t0
        return stats
