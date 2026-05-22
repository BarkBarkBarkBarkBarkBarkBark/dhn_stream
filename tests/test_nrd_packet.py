"""
Tests for the NRD wire-format builder/parser in `udp_raw/nrd_sender.py`.

These verify internal self-consistency (round-trip pack/unpack + CRC) and
the on-wire size formula. They do NOT verify against a real Pegasus capture
— that ground-truth check is documented in `docs/smoke_tests.yaml` and must
be run manually before declaring the format `verified`.
"""
from __future__ import annotations

import numpy as np
import pytest

from darkhorse_neuralynx.udp_raw.nrd_file import detect_nrd_file, iter_nrd_samples
from darkhorse_neuralynx.udp_raw.nrd_sender import (
    NRD_CRC_SIZE_BYTES,
    NRD_HEADER_SIZE_BYTES,
    NRD_STX,
    NrdUdpSender,
    build_nrd_packet,
    nrd_packet_size_bytes,
    parse_nrd_packet,
    _xor_crc_int32,
)


@pytest.mark.parametrize("n_channels", [1, 16, 64, 256, 512])
def test_packet_size_matches_formula(n_channels: int) -> None:
    samples = np.arange(n_channels, dtype="<i4")
    pkt = build_nrd_packet(packet_id=0, timestamp_us=0, samples_int32=samples)
    expected = NRD_HEADER_SIZE_BYTES + 4 * n_channels + NRD_CRC_SIZE_BYTES
    assert len(pkt) == expected == nrd_packet_size_bytes(n_channels)


def test_stx_is_first_four_bytes() -> None:
    pkt = build_nrd_packet(0, 0, np.zeros(16, dtype="<i4"))
    assert pkt[:4] == NRD_STX.to_bytes(4, "little") == bytes.fromhex("00080000")


def test_round_trip_basic() -> None:
    samples = np.array([-1000, 0, 1, 32767, -32768, 12345, -42, 999_999,
                        2**30, -(2**30), 7, 8, 9, 10, 11, 12], dtype="<i4")
    pkt = build_nrd_packet(
        packet_id=0xDEADBEEF & 0x7FFFFFFF,
        timestamp_us=0x0123_4567_89AB_CDEF,
        samples_int32=samples,
        status=42,
        parallel_port=0xCAFEBABE,
        extras=np.arange(10, dtype="<i4") * 1000,
    )
    decoded = parse_nrd_packet(pkt)
    assert decoded["stx"] == NRD_STX
    assert decoded["packet_id"] == (0xDEADBEEF & 0x7FFFFFFF)
    assert decoded["timestamp_us"] == 0x0123_4567_89AB_CDEF
    assert decoded["status"] == 42
    assert decoded["parallel_port"] == 0xCAFEBABE
    assert np.array_equal(decoded["extras"], np.arange(10, dtype="<i4") * 1000)
    assert np.array_equal(decoded["samples"], samples)
    assert decoded["n_channels"] == samples.size
    # size_int32_words = 14 status words + n_channels samples
    assert decoded["size_int32_words"] == 14 + samples.size


def test_size_word_value() -> None:
    for n in [1, 16, 512]:
        pkt = build_nrd_packet(0, 0, np.zeros(n, dtype="<i4"))
        decoded = parse_nrd_packet(pkt)
        assert decoded["size_int32_words"] == 14 + n


def test_crc_detects_bit_flip() -> None:
    pkt = bytearray(build_nrd_packet(7, 1234, np.arange(16, dtype="<i4")))
    # Flip a byte inside the samples region
    pkt[NRD_HEADER_SIZE_BYTES + 3] ^= 0x01
    with pytest.raises(ValueError, match="CRC mismatch"):
        parse_nrd_packet(bytes(pkt))


def test_bad_stx_rejected() -> None:
    pkt = bytearray(build_nrd_packet(0, 0, np.zeros(4, dtype="<i4")))
    pkt[0] = 0xFF
    # Recompute CRC so we isolate the STX-failure path
    crc = _xor_crc_int32(bytes(pkt[:-4]))
    pkt[-4:] = crc.to_bytes(4, "little")
    with pytest.raises(ValueError, match="bad STX"):
        parse_nrd_packet(bytes(pkt))


def test_timestamp_round_trip_zero_and_max() -> None:
    for ts in [0, 1, 1_000_000, 2**32 - 1, 2**32, 2**63, 2**64 - 1]:
        pkt = build_nrd_packet(0, ts, np.zeros(2, dtype="<i4"))
        assert parse_nrd_packet(pkt)["timestamp_us"] == ts


def test_extras_default_zero() -> None:
    pkt = build_nrd_packet(0, 0, np.zeros(8, dtype="<i4"))
    decoded = parse_nrd_packet(pkt)
    assert np.all(decoded["extras"] == 0)
    assert decoded["extras"].dtype == np.int32
    assert decoded["extras"].size == 10


def test_extras_wrong_size_rejected() -> None:
    with pytest.raises(ValueError, match="extras must have"):
        build_nrd_packet(0, 0, np.zeros(2, dtype="<i4"), extras=np.zeros(5, dtype="<i4"))


def test_packet_too_short_rejected() -> None:
    with pytest.raises(ValueError, match="too short"):
        parse_nrd_packet(b"\x00" * 8)


def test_payload_misaligned_rejected() -> None:
    # 68 + 4*N + 4 must hold; an odd extra byte breaks it
    pkt = build_nrd_packet(0, 0, np.zeros(4, dtype="<i4")) + b"\x00"
    with pytest.raises(ValueError, match="not int32-aligned"):
        parse_nrd_packet(pkt)


def test_xor_crc_known_value() -> None:
    # XOR of [0xAAAAAAAA, 0x55555555] = 0xFFFFFFFF
    buf = (0xAAAAAAAA).to_bytes(4, "little") + (0x55555555).to_bytes(4, "little")
    assert _xor_crc_int32(buf) == 0xFFFFFFFF
    # XOR of identical words = 0
    assert _xor_crc_int32(b"\xDE\xAD\xBE\xEF" * 2) == 0


class _FakeSocket:
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, pkt: bytes, _addr: tuple[str, int]) -> int:
        self.packets.append(pkt)
        return len(pkt)


def test_sender_fractional_timestamp_accumulator_32khz() -> None:
    sender = NrdUdpSender("127.0.0.1", 26090)
    fake_socket = _FakeSocket()
    sender._sock = fake_socket
    sender._next_send = 0.0

    chunk = np.zeros((32_000, 1), dtype="<i4")
    stats = sender.send_chunk(chunk, sample_rate_hz=32_000)

    assert stats.packets_sent == 32_000
    assert sender._timestamp_us == 1_000_000
    assert parse_nrd_packet(fake_socket.packets[0])["timestamp_us"] == 0
    assert parse_nrd_packet(fake_socket.packets[-1])["timestamp_us"] == 999_968


def test_sender_mirror_target_counts_mirrored_packets() -> None:
    sender = NrdUdpSender("127.0.0.1", 26090, mirror_targets=(("127.0.0.1", 26091),))
    fake_socket = _FakeSocket()
    sender._sock = fake_socket
    sender._next_send = 0.0

    stats = sender.send_chunk(np.zeros((2, 3), dtype="<i4"), sample_rate_hz=32_000)

    assert stats.packets_sent == 2
    assert stats.mirror_packets_sent == 2
    assert len(fake_socket.packets) == 4


def test_zero_primer_handoff_keeps_packet_ids_and_timestamps_monotonic() -> None:
    sender = NrdUdpSender("127.0.0.1", 26090)
    fake_socket = _FakeSocket()
    sender._sock = fake_socket
    sender._next_send = 0.0

    primer = np.zeros((3, 4), dtype="<i4")
    replay = np.array([[10, 20, 30, 40], [50, 60, 70, 80]], dtype="<i4")

    sender.send_chunk(primer, sample_rate_hz=32_000)
    sender.send_chunk(replay, sample_rate_hz=32_000)

    decoded = [parse_nrd_packet(packet) for packet in fake_socket.packets]
    assert [packet["packet_id"] for packet in decoded] == [0, 1, 2, 3, 4]
    assert [packet["timestamp_us"] for packet in decoded] == sorted(
        packet["timestamp_us"] for packet in decoded
    )
    assert np.array_equal(decoded[0]["samples"], np.zeros(4, dtype="<i4"))
    assert np.array_equal(decoded[3]["samples"], replay[0])


@pytest.mark.parametrize(
    "n_channels,expected",
    [(16, 136), (64, 328), (256, 1096), (512, 2120)],
)
def test_size_table_matches_smoke_yaml(n_channels: int, expected: int) -> None:
    """Sizes must match `docs/smoke_tests.yaml` scenarios."""
    assert nrd_packet_size_bytes(n_channels) == expected


def test_nrd_file_iterator_stops_at_eof(tmp_path) -> None:
    path = tmp_path / "tiny.nrd"
    first = np.array([1, 2], dtype="<i4")
    second = np.array([3, 4], dtype="<i4")
    path.write_bytes(
        b"######## NRD header ########\n"
        + build_nrd_packet(0, 0, first)
        + build_nrd_packet(1, 31, second)
    )

    layout = detect_nrd_file(path, scan_bytes=1024)
    chunks = list(iter_nrd_samples(layout, batch_packets=1))

    assert layout.n_channels == 2
    assert layout.packet_count == 2
    assert len(chunks) == 2
    assert np.array_equal(chunks[0], first.reshape(1, 2))
    assert np.array_equal(chunks[1], second.reshape(1, 2))
