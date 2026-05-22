from __future__ import annotations

import json

import numpy as np
from typer.testing import CliRunner

from darkhorse_neuralynx.cli import app
from darkhorse_neuralynx.udp_raw.nrd_sender import build_nrd_packet
from darkhorse_neuralynx.udp_raw.nrd_stats import compute_nrd_stats


def _write_tiny_nrd(path) -> None:
    path.write_bytes(
        b"######## NRD header ########\n"
        b"channel 1 micro\n"
        b"channel 2 macro\n"
        + build_nrd_packet(0, 0, np.array([0, 10], dtype="<i4"))
        + build_nrd_packet(1, 31, np.array([0, -10], dtype="<i4"))
        + build_nrd_packet(2, 62, np.array([0, 30], dtype="<i4"))
    )


def test_compute_nrd_stats_per_channel(tmp_path) -> None:
    path = tmp_path / "tiny.nrd"
    _write_tiny_nrd(path)

    report = compute_nrd_stats(path, sample_rate_hz=32_000)

    assert report.n_channels == 2
    assert report.packets_analyzed == 3
    assert report.estimated_duration_seconds == 3 / 32_000
    assert report.header_mentions_micro is True
    assert report.header_mentions_macro is True

    first, second = report.channels
    assert first.minimum == 0
    assert first.maximum == 0
    assert first.mean == 0.0
    assert first.std == 0.0
    assert first.quality == "flat"
    assert first.electrode_type == "micro"

    assert second.minimum == -10
    assert second.maximum == 30
    assert second.mean == 10.0
    assert round(second.rms, 6) == round(float(np.sqrt((10**2 + (-10) ** 2 + 30**2) / 3)), 6)
    assert second.quality == "noise-like"
    assert second.electrode_type == "macro"


def test_stats_cli_writes_json(tmp_path) -> None:
    path = tmp_path / "tiny.nrd"
    report_path = tmp_path / "report.json"
    _write_tiny_nrd(path)

    result = CliRunner().invoke(
        app,
        [
            "stats",
            "--file",
            str(path),
            "--sample-rate",
            "32000",
            "--max-packets",
            "2",
            "--max-rows",
            "0",
            "--report-json",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert "NRD Statistics" in result.output
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["packets_analyzed"] == 2
    assert payload["n_channels"] == 2
    assert payload["channels"][0]["quality"] == "flat"
