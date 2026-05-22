from __future__ import annotations

import numpy as np
import pytest

from darkhorse_neuralynx.dashboard.labels import load_channel_config_csv, merge_labels
from darkhorse_neuralynx.dashboard.monitor import LiveNrdMonitor, min_max_downsample
from darkhorse_neuralynx.udp_raw.nrd_sender import build_nrd_packet


def test_channel_config_labels_load_from_csv(tmp_path) -> None:
    path = tmp_path / "DHN_Acq_cs.csv"
    path.write_text(
        "Channel Number,Channel Name,Channel Description\n"
        "1,g1_0001,first contact\n"
        "2,g1_0002,second contact\n",
        encoding="utf-8",
    )

    labels = load_channel_config_csv(path)

    assert labels[1].name == "g1_0001"
    assert labels[1].description == "first contact"
    assert labels[2].name == "g1_0002"


def test_merge_labels_fills_missing_channels(tmp_path) -> None:
    path = tmp_path / "channels.csv"
    path.write_text("Channel Number,Channel Name\n2,named_two\n", encoding="utf-8")

    labels = merge_labels(3, load_channel_config_csv(path))

    assert [label.name for label in labels] == ["ch0001", "named_two", "ch0003"]


def test_min_max_downsample_preserves_extrema() -> None:
    values = np.array([0, 10, -20, 5, 100, -100, 2, 3], dtype="<i4")

    minima, maxima = min_max_downsample(values, target_bins=2)

    assert np.array_equal(minima, np.array([-20, -100], dtype="<i4"))
    assert np.array_equal(maxima, np.array([10, 100], dtype="<i4"))


def test_live_monitor_updates_stats_and_waveform() -> None:
    monitor = LiveNrdMonitor(expected_channels=2, sample_rate_hz=32_000, waveform_seconds=0.001)

    monitor.update_packet(build_nrd_packet(0, 0, np.array([0, 10], dtype="<i4")), received_at=1.0)
    monitor.update_packet(build_nrd_packet(1, 31, np.array([0, -10], dtype="<i4")), received_at=1.001)
    monitor.update_packet(build_nrd_packet(2, 62, np.array([0, 30], dtype="<i4")), received_at=1.002)

    snapshot = monitor.snapshot()
    assert snapshot["packet_count"] == 3
    assert snapshot["parse_errors"] == 0
    assert snapshot["channels"][0]["quality"] == "flat"
    assert snapshot["channels"][1]["minimum"] == -10
    assert snapshot["channels"][1]["maximum"] == 30
    assert snapshot["channels"][1]["quality"] == "noise-like"

    wave = monitor.waveform(2, bins=2)
    assert wave.samples_seen == 3
    assert wave.min_values
    assert wave.max_values


def test_live_monitor_counts_parse_errors() -> None:
    monitor = LiveNrdMonitor(expected_channels=2)

    monitor.update_packet(b"not an nrd packet", received_at=1.0)

    snapshot = monitor.snapshot()
    assert snapshot["parse_errors"] == 1
    assert snapshot["packet_count"] == 0


def test_dashboard_app_import_guard() -> None:
    pytest.importorskip("fastapi")
    from darkhorse_neuralynx.dashboard.app import create_dashboard_app

    app = create_dashboard_app(udp_host="127.0.0.1", udp_port=0, sample_rate_hz=32_000)
    try:
        assert app.title == "DHN Waveform Inspector"
    finally:
        app.state.monitor.stop()
