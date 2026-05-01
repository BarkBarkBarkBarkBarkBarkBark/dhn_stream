"""
Pydantic configuration models for the DHN-AQ UDP acquisition pipeline.

Load with AppConfig.from_yaml("path/to/config.yaml").
CLI flags should override individual fields after loading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class SessionConfig(BaseModel):
    name: str = "unnamed_session"
    description: str = ""


class UdpConfig(BaseModel):
    host: str = "192.168.3.50"
    port: int = 26090
    broadcast: bool = False
    send_buffer_bytes: int = 8_388_608


class SignalConfig(BaseModel):
    source: str = "spikeinterface_generate_ground_truth_recording"
    channels: int = 16
    units: int = 8
    sample_rate_hz: int = 32000
    duration_seconds: float = 60.0
    seed: int = 42


class PayloadConfig(BaseModel):
    dtype: Literal["int16"] = "int16"
    endianness: Literal["little", "big"] = "little"
    layout: Literal["sample_major", "channel_major"] = "sample_major"
    frames_per_packet: int = Field(default=1, ge=1)
    target_peak_int16: int = Field(default=8000, ge=1, le=32767)
    headerless: bool = True


class PacingConfig(BaseModel):
    realtime: bool = True
    speed: float = Field(default=1.0, gt=0.0)


class DhnAqConfig(BaseModel):
    expected_receive_mode: str = "pure_udp_no_header"
    expected_channel_count: int = 16
    expected_sample_rate_hz: int = 32000
    notes: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    session: SessionConfig = Field(default_factory=SessionConfig)
    udp: UdpConfig = Field(default_factory=UdpConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    dhn_aq: DhnAqConfig = Field(default_factory=DhnAqConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        """Load config from a YAML file. Raises FileNotFoundError if missing."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        with p.open() as fh:
            data = yaml.safe_load(fh)
        return cls.model_validate(data or {})
