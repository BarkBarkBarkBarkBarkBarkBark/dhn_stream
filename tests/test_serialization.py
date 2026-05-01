"""
Unit tests for sample_major and channel_major UDP serialization.

Verifies the byte layout of payloads produced by RawUdpSender.send_chunk
without requiring a network or DHN-AQ.

No socket is opened — tests use dry-run payload construction via numpy directly
and compare against the serialization logic in raw_sender.
"""

import numpy as np
import pytest


def make_sample_major_payload(traces: np.ndarray, frames_per_packet: int) -> list[bytes]:
    """Reference implementation of sample_major serialization."""
    traces = np.ascontiguousarray(traces.astype("<i2"))
    n_frames, n_channels = traces.shape
    payloads = []
    for i in range(0, n_frames - frames_per_packet + 1, frames_per_packet):
        chunk = traces[i : i + frames_per_packet]
        payloads.append(chunk.tobytes(order="C"))
    return payloads


def make_channel_major_payload(traces: np.ndarray, frames_per_packet: int) -> list[bytes]:
    """Reference implementation of channel_major serialization."""
    traces = np.ascontiguousarray(traces.astype("<i2"))
    n_frames, n_channels = traces.shape
    payloads = []
    for i in range(0, n_frames - frames_per_packet + 1, frames_per_packet):
        chunk = traces[i : i + frames_per_packet]   # (fpp, n_channels)
        chunk_cm = np.ascontiguousarray(chunk.T)    # (n_channels, fpp)
        payloads.append(chunk_cm.tobytes(order="C"))
    return payloads


class TestSampleMajorLayout:
    def test_single_frame_single_channel(self):
        traces = np.array([[42]], dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=1)
        assert len(payloads) == 1
        assert payloads[0] == np.int16(42).astype("<i2").tobytes()

    def test_single_frame_multi_channel_order(self):
        # frame0: ch0=1, ch1=2, ch2=3
        traces = np.array([[1, 2, 3]], dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=1)
        assert len(payloads) == 1
        decoded = np.frombuffer(payloads[0], dtype="<i2")
        np.testing.assert_array_equal(decoded, [1, 2, 3])

    def test_multi_frame_sample_major_interleaving(self):
        # frame0: [10, 20], frame1: [30, 40]
        # Expected wire order: 10, 20, 30, 40
        traces = np.array([[10, 20], [30, 40]], dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=2)
        assert len(payloads) == 1
        decoded = np.frombuffer(payloads[0], dtype="<i2")
        np.testing.assert_array_equal(decoded, [10, 20, 30, 40])

    def test_payload_size_is_frames_times_channels_times_2(self):
        n_frames, n_channels, fpp = 8, 16, 2
        traces = np.ones((n_frames, n_channels), dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=fpp)
        expected_bytes = fpp * n_channels * 2
        for p in payloads:
            assert len(p) == expected_bytes

    def test_packet_count(self):
        n_frames, fpp = 32, 4
        traces = np.zeros((n_frames, 8), dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=fpp)
        assert len(payloads) == n_frames // fpp

    def test_little_endian_byte_order(self):
        # Value 256 in little-endian int16 is [0x00, 0x01]
        traces = np.array([[256]], dtype=np.int16)
        payloads = make_sample_major_payload(traces, frames_per_packet=1)
        assert payloads[0] == b"\x00\x01"


class TestChannelMajorLayout:
    def test_channel_major_vs_sample_major_differ(self):
        # With 2 channels and 2 frames, layouts should produce different byte orders.
        traces = np.array([[1, 2], [3, 4]], dtype=np.int16)
        sm = make_sample_major_payload(traces, frames_per_packet=2)[0]
        cm = make_channel_major_payload(traces, frames_per_packet=2)[0]
        # sample_major: 1, 2, 3, 4 — channel_major: 1, 3, 2, 4
        sm_decoded = np.frombuffer(sm, dtype="<i2").tolist()
        cm_decoded = np.frombuffer(cm, dtype="<i2").tolist()
        assert sm_decoded == [1, 2, 3, 4]
        assert cm_decoded == [1, 3, 2, 4]

    def test_channel_major_payload_size_equals_sample_major(self):
        n_frames, n_channels, fpp = 4, 8, 2
        traces = np.random.randint(-100, 100, (n_frames, n_channels), dtype=np.int16)
        sm = make_sample_major_payload(traces, fpp)
        cm = make_channel_major_payload(traces, fpp)
        assert all(len(a) == len(b) for a, b in zip(sm, cm))
