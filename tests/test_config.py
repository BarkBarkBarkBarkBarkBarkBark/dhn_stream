"""
Unit tests for AppConfig YAML loading.

No network, no DHN-AQ required.
"""

import textwrap
from pathlib import Path

import pytest

from darkhorse_neuralynx.config.models import AppConfig


EXAMPLE_YAML = textwrap.dedent("""\
    session:
      name: synthetic_udp_16ch_test
      description: SpikeInterface synthetic recording sent as headerless UDP into DHN-AQ.

    udp:
      host: "192.168.3.50"
      port: 26090
      broadcast: false
      send_buffer_bytes: 8388608

    signal:
      source: spikeinterface_generate_ground_truth_recording
      channels: 16
      units: 8
      sample_rate_hz: 32000
      duration_seconds: 60
      seed: 42

    payload:
      dtype: int16
      endianness: little
      layout: sample_major
      frames_per_packet: 1
      target_peak_int16: 8000
      headerless: true

    pacing:
      realtime: true
      speed: 1.0

    dhn_aq:
      expected_receive_mode: pure_udp_no_header
      expected_channel_count: 16
      expected_sample_rate_hz: 32000
      notes:
        - Configure DHN-AQ channel spec to exactly match channel count and sample rate.
""")


@pytest.fixture()
def yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "test_config.yaml"
    p.write_text(EXAMPLE_YAML)
    return p


def test_from_yaml_loads_session(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.session.name == "synthetic_udp_16ch_test"


def test_from_yaml_loads_udp(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.udp.host == "192.168.3.50"
    assert cfg.udp.port == 26090
    assert cfg.udp.broadcast is False
    assert cfg.udp.send_buffer_bytes == 8_388_608


def test_from_yaml_loads_signal(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.signal.channels == 16
    assert cfg.signal.sample_rate_hz == 32000
    assert cfg.signal.duration_seconds == 60.0
    assert cfg.signal.seed == 42


def test_from_yaml_loads_payload(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.payload.dtype == "int16"
    assert cfg.payload.endianness == "little"
    assert cfg.payload.layout == "sample_major"
    assert cfg.payload.frames_per_packet == 1
    assert cfg.payload.target_peak_int16 == 8000
    assert cfg.payload.headerless is True


def test_from_yaml_loads_pacing(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.pacing.realtime is True
    assert cfg.pacing.speed == 1.0


def test_from_yaml_loads_dhn_aq(yaml_file: Path):
    cfg = AppConfig.from_yaml(yaml_file)
    assert cfg.dhn_aq.expected_channel_count == 16
    assert cfg.dhn_aq.expected_sample_rate_hz == 32000


def test_from_yaml_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        AppConfig.from_yaml("/nonexistent/path/config.yaml")


def test_defaults_when_no_yaml():
    cfg = AppConfig()
    assert cfg.udp.host == "192.168.3.50"
    assert cfg.udp.port == 26090
    assert cfg.signal.channels == 16
    assert cfg.signal.sample_rate_hz == 32000
    assert cfg.payload.dtype == "int16"
    assert cfg.payload.layout == "sample_major"


def test_partial_yaml_uses_defaults(tmp_path: Path):
    p = tmp_path / "partial.yaml"
    p.write_text("udp:\n  port: 9999\n")
    cfg = AppConfig.from_yaml(p)
    assert cfg.udp.port == 9999
    # Everything else defaults
    assert cfg.udp.host == "192.168.3.50"
    assert cfg.signal.channels == 16
