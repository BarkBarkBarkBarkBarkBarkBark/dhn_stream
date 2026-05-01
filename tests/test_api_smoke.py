"""
API smoke tests for the DHN dashboard REST endpoints.

Run with:
    cd /path/to/darkhorse_stream
    pytest tests/test_api_smoke.py -v

Requires pytest-django:
    pip install pytest-django

DJANGO_SETTINGS_MODULE is set in pyproject.toml / pytest.ini.
"""

import io
import json

import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client()


# ── /api/sources/ ──────────────────────────────────────────────────────────

class TestApiSources:
    def test_returns_200(self, client):
        r = client.get("/api/sources/")
        assert r.status_code == 200

    def test_has_sources_key(self, client):
        r = client.get("/api/sources/")
        data = r.json()
        assert "sources" in data

    def test_has_four_sources(self, client):
        r = client.get("/api/sources/")
        sources = r.json()["sources"]
        assert len(sources) == 4

    def test_source_ids(self, client):
        r = client.get("/api/sources/")
        ids = {s["id"] for s in r.json()["sources"]}
        assert ids == {"sine_harmonics", "spikeinterface", "udp_passthrough", "file_replay"}

    def test_each_source_has_required_fields(self, client):
        r = client.get("/api/sources/")
        for src in r.json()["sources"]:
            assert "id" in src
            assert "label" in src
            assert "description" in src
            assert "params" in src

    def test_post_not_allowed(self, client):
        r = client.post("/api/sources/")
        assert r.status_code == 405


# ── /api/preview/ ──────────────────────────────────────────────────────────

class TestApiPreview:
    def _preview(self, client, source, extra=None):
        cfg = {"sample_rate_hz": 8000, "channels": 4, "fundamental_hz": 440}
        if extra:
            cfg.update(extra)
        return client.post(
            "/api/preview/",
            data=json.dumps({"source": source, "config": cfg}),
            content_type="application/json",
        )

    def test_sine_returns_200(self, client):
        r = self._preview(client, "sine_harmonics")
        assert r.status_code == 200

    def test_sine_response_shape(self, client):
        r = self._preview(client, "sine_harmonics")
        data = r.json()
        assert "time_s" in data
        assert "channels" in data
        assert "channel_labels" in data
        assert "sample_rate_hz" in data
        assert "preview_seconds" in data
        assert "properties" in data

    def test_sine_channel_count(self, client):
        r = self._preview(client, "sine_harmonics", extra={"channels": 4})
        data = r.json()
        assert len(data["channels"]) == 4

    def test_sine_time_length(self, client):
        r = self._preview(client, "sine_harmonics",
                           extra={"sample_rate_hz": 8000})
        data = r.json()
        # 0.5 s at 8000 Hz = 4000 samples
        assert len(data["time_s"]) == 4000

    def test_sine_normalised_to_minus1_plus1(self, client):
        r = self._preview(client, "sine_harmonics")
        data = r.json()
        for ch in data["channels"]:
            assert max(abs(v) for v in ch) <= 1.01  # allow fp rounding

    def test_spikeinterface_returns_200(self, client):
        r = self._preview(client, "spikeinterface",
                           extra={"channels": 4, "units": 4, "seed": 0})
        assert r.status_code == 200

    def test_spikeinterface_has_channels(self, client):
        r = self._preview(client, "spikeinterface",
                           extra={"channels": 4, "units": 4, "seed": 0})
        data = r.json()
        assert len(data["channels"]) == 4

    def test_unknown_source_returns_400(self, client):
        r = client.post(
            "/api/preview/",
            data=json.dumps({"source": "nonexistent", "config": {}}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_invalid_json_returns_400(self, client):
        r = client.post("/api/preview/", data="not-json",
                        content_type="application/json")
        assert r.status_code == 400

    def test_file_replay_missing_file_returns_500(self, client):
        r = self._preview(client, "file_replay",
                           extra={"file_path": "/nonexistent/path.bin", "channels": 4})
        assert r.status_code == 500

    def test_get_not_allowed(self, client):
        r = client.get("/api/preview/")
        assert r.status_code == 405


# ── /api/files/ ────────────────────────────────────────────────────────────

class TestApiFiles:
    def test_uploads_returns_200(self, client):
        r = client.get("/api/files/?dir=uploads")
        assert r.status_code == 200

    def test_uploads_has_entries_key(self, client):
        r = client.get("/api/files/?dir=uploads")
        data = r.json()
        assert "entries" in data
        assert "directory" in data

    def test_invalid_dir_returns_400(self, client):
        r = client.get("/api/files/?dir=../../etc")
        assert r.status_code == 400

    def test_unknown_dir_returns_400(self, client):
        r = client.get("/api/files/?dir=secrets")
        assert r.status_code == 400

    def test_post_not_allowed(self, client):
        r = client.post("/api/files/")
        assert r.status_code == 405


# ── /api/upload/ ───────────────────────────────────────────────────────────

class TestApiUpload:
    def test_no_file_returns_400(self, client):
        r = client.post("/api/upload/")
        assert r.status_code == 400

    def test_wrong_extension_returns_400(self, client):
        fake = io.BytesIO(b"\x00" * 16)
        fake.name = "test.mp3"
        r = client.post("/api/upload/", data={"file": fake})
        assert r.status_code == 400

    def test_valid_bin_upload(self, client, tmp_path):
        import numpy as np
        import os
        from pathlib import Path

        payload = np.zeros(64, dtype="<i2").tobytes()
        fake = io.BytesIO(payload)
        fake.name = "smoke_test.bin"
        r = client.post("/api/upload/", data={"file": fake})
        assert r.status_code == 200
        data = r.json()
        assert "path" in data
        assert "size_bytes" in data
        assert data["size_bytes"] == 128  # 64 int16 = 128 bytes
        # Clean up
        try:
            Path(data["path"]).unlink(missing_ok=True)
        except Exception:
            pass

    def test_get_not_allowed(self, client):
        r = client.get("/api/upload/")
        assert r.status_code == 405


# ── /api/status/ ───────────────────────────────────────────────────────────

class TestApiStatus:
    def test_returns_200(self, client):
        r = client.get("/api/status/")
        assert r.status_code == 200

    def test_has_sender_and_receiver(self, client):
        r = client.get("/api/status/")
        data = r.json()
        assert "sender" in data
        assert "receiver" in data

    def test_sender_has_running_key(self, client):
        r = client.get("/api/status/")
        assert "running" in r.json()["sender"]

    def test_receiver_has_running_key(self, client):
        r = client.get("/api/status/")
        assert "running" in r.json()["receiver"]

    def test_both_stopped_on_boot(self, client):
        r = client.get("/api/status/")
        data = r.json()
        assert data["sender"]["running"] is False
        assert data["receiver"]["running"] is False

    def test_post_not_allowed(self, client):
        r = client.post("/api/status/")
        assert r.status_code == 405
