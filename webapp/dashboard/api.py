"""
REST-style JSON API views for the DHN dashboard.

Endpoints:
  GET  /api/sources/                  — list available source types
  POST /api/preview/                  — generate waveform preview (first 0.1s)
  GET  /api/files/?dir=<path>         — list files in a directory
  POST /api/upload/                   — upload a raw int16 binary file
  GET  /api/status/                   — sender + receiver running state

All responses are JSON. No auth. Local lab use only.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from dashboard import state

# Where uploaded files are stored (relative to repo root)
_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
_UPLOAD_DIR.mkdir(exist_ok=True)

# Allowed directories the browser can browse (security: whitelist only)
_BROWSE_ROOTS = [
    Path(__file__).resolve().parent.parent.parent / "uploads",
    Path(__file__).resolve().parent.parent.parent / "configs",
]


# ── /api/sources/ ─────────────────────────────────────────────────────────────

@require_GET
def api_sources(request):
    return JsonResponse({
        "sources": [
            {
                "id": "sine_harmonics",
                "label": "Synthetic — Harmonic Sine Waves",
                "description": (
                    "4-channel signal. Ch 0: fundamental only. "
                    "Ch 1: f1+f2. Ch 2: f1-f3. Ch 3: f1-f4. "
                    "Generated in real time, no file required."
                ),
                "params": ["fundamental_hz", "channels", "sample_rate_hz",
                           "frames_per_packet", "target_peak"],
            },
            {
                "id": "spikeinterface",
                "label": "Synthetic — SpikeInterface Ground-Truth",
                "description": (
                    "Uses spikeinterface.generate_ground_truth_recording to produce "
                    "realistic extracellular spike data."
                ),
                "params": ["channels", "sample_rate_hz", "duration_seconds",
                           "seed", "frames_per_packet", "target_peak"],
            },
            {
                "id": "udp_passthrough",
                "label": "Network Source — Forward Incoming UDP",
                "description": (
                    "Listen on a local UDP port and re-stream raw packets to a destination. "
                    "Useful when the source is another machine on the network."
                ),
                "params": ["bind_host", "receiver_port", "dest_host", "dest_port"],
            },
            {
                "id": "file_replay",
                "label": "File Replay — Raw int16 Binary",
                "description": (
                    "Upload or select a flat binary file of int16 samples "
                    "(sample-major, little-endian). Replayed at the configured sample rate."
                ),
                "params": ["file_path", "channels", "sample_rate_hz",
                           "frames_per_packet", "target_peak"],
            },
        ]
    })


# ── /api/preview/ ─────────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def api_preview(request):
    """
    Generate a short waveform preview (first 0.5 s) for the configured source.

    Request body JSON:
      { "source": "sine_harmonics"|"spikeinterface"|"file_replay",
        "config": { ... } }

    Response JSON:
      {
        "time_s":   [0.0, 0.0001, ...],          # time axis
        "channels": [[ch0 samples], [ch1], ...],  # float64, normalised to [-1, 1]
        "channel_labels": ["Ch 0 (f1)", ...],
        "sample_rate_hz": 32000,
        "preview_seconds": 0.5,
        "properties": { ... }                     # source-specific metadata
      }
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    source = body.get("source", "sine_harmonics")
    cfg = body.get("config", {})

    sr = int(cfg.get("sample_rate_hz", 32000))
    preview_seconds = 0.5
    n_frames = int(sr * preview_seconds)

    try:
        if source == "sine_harmonics":
            data, labels, props = _preview_sine(cfg, n_frames, sr)

        elif source == "spikeinterface":
            data, labels, props = _preview_spikeinterface(cfg, n_frames, sr)

        elif source == "file_replay":
            data, labels, props = _preview_file(cfg, n_frames)
            sr = props.get("sample_rate_hz", sr)

        else:
            return JsonResponse({"error": f"unknown source: {source}"}, status=400)

    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    time_s = (np.arange(n_frames) / sr).tolist()

    return JsonResponse({
        "time_s": time_s,
        "channels": data,
        "channel_labels": labels,
        "sample_rate_hz": sr,
        "preview_seconds": preview_seconds,
        "properties": props,
    })


def _preview_sine(cfg: dict, n_frames: int, sr: int):
    n_ch = int(cfg.get("channels", 4))
    fundamental = float(cfg.get("fundamental_hz", 440.0))
    t = np.linspace(0, n_frames / sr, n_frames, endpoint=False)

    data = []
    labels = []
    for ch in range(n_ch):
        n_harmonics = ch + 1 if ch < 4 else 0
        sig = np.zeros(n_frames)
        for h in range(1, n_harmonics + 1):
            sig += np.sin(2 * math.pi * fundamental * h * t)
        peak = np.max(np.abs(sig)) or 1.0
        data.append((sig / peak).tolist())

        if ch < 4:
            harm_str = " + ".join(
                f"{int(fundamental * h)} Hz" for h in range(1, n_harmonics + 1)
            )
            labels.append(f"Ch {ch}  ({harm_str})")
        else:
            labels.append(f"Ch {ch}  (silence)")

    props = {
        "fundamental_hz": fundamental,
        "n_harmonics_per_channel": [min(ch + 1, 4) if ch < 4 else 0 for ch in range(n_ch)],
        "dtype": "int16",
        "layout": "sample_major",
    }
    return data, labels, props


def _preview_spikeinterface(cfg: dict, n_frames: int, sr: int):
    import spikeinterface.core as si  # lazy import

    n_ch = int(cfg.get("channels", 16))
    units = int(cfg.get("units", 8))
    seed = int(cfg.get("seed", 42))
    dur = max(1.0, n_frames / sr + 0.1)

    recording, _ = si.generate_ground_truth_recording(
        durations=[dur],
        sampling_frequency=float(sr),
        num_channels=n_ch,
        num_units=units,
        seed=seed,
    )
    traces = recording.get_traces(start_frame=0, end_frame=n_frames,
                                   segment_index=0, return_scaled=True)
    # Normalise each channel independently
    data = []
    for ch in range(n_ch):
        col = traces[:, ch].astype(float)
        peak = np.max(np.abs(col)) or 1.0
        data.append((col / peak).tolist())

    labels = [f"Ch {i}" for i in range(n_ch)]
    props = {"source": "spikeinterface_generate_ground_truth_recording",
             "seed": seed, "units": units}
    return data, labels, props


def _preview_file(cfg: dict, n_frames: int):
    file_path = cfg.get("file_path", "")
    n_ch = int(cfg.get("channels", 16))
    sr = int(cfg.get("sample_rate_hz", 32000))

    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if p.suffix not in (".bin", ".dat", ".raw", ".int16"):
        raise ValueError(f"Unsupported file extension: {p.suffix}")

    raw = np.fromfile(p, dtype="<i2")
    max_frames = len(raw) // n_ch
    if max_frames == 0:
        raise ValueError("File too small for given channel count")

    frames = min(n_frames, max_frames)
    arr = raw[: frames * n_ch].reshape(frames, n_ch).astype(float)
    data = []
    for ch in range(n_ch):
        col = arr[:, ch]
        peak = np.max(np.abs(col)) or 1.0
        data.append((col / peak).tolist())

    labels = [f"Ch {i}" for i in range(n_ch)]
    props = {
        "file_path": str(p),
        "file_size_bytes": p.stat().st_size,
        "total_frames": max_frames,
        "channels": n_ch,
        "sample_rate_hz": sr,
        "duration_seconds": round(max_frames / sr, 3),
    }
    return data, labels, props


# ── /api/files/ ───────────────────────────────────────────────────────────────

@require_GET
def api_files(request):
    """
    List files in a whitelisted directory.

    Query params:
      ?dir=uploads   (must match a _BROWSE_ROOTS entry by name)
    """
    dir_name = request.GET.get("dir", "uploads")
    root = next((r for r in _BROWSE_ROOTS if r.name == dir_name), None)
    if root is None:
        return JsonResponse({"error": f"directory '{dir_name}' is not browsable"}, status=400)

    entries = []
    for p in sorted(root.iterdir()):
        entries.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": p.stat().st_size if p.is_file() else None,
            "is_dir": p.is_dir(),
            "extension": p.suffix,
        })

    return JsonResponse({"directory": str(root), "entries": entries})


# ── /api/upload/ ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def api_upload(request):
    """
    Upload a raw binary file (.bin, .dat, .raw, .int16).

    Multipart POST with field name: file
    Returns: { "path": "<absolute path>", "size_bytes": N }
    """
    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error": "no file in request"}, status=400)

    ext = Path(uploaded.name).suffix.lower()
    if ext not in (".bin", ".dat", ".raw", ".int16"):
        return JsonResponse(
            {"error": f"unsupported file type '{ext}'. Allowed: .bin .dat .raw .int16"},
            status=400,
        )

    dest = _UPLOAD_DIR / uploaded.name
    # Avoid path traversal
    if not str(dest.resolve()).startswith(str(_UPLOAD_DIR.resolve())):
        return JsonResponse({"error": "invalid filename"}, status=400)

    with dest.open("wb") as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)

    return JsonResponse({
        "path": str(dest),
        "filename": uploaded.name,
        "size_bytes": dest.stat().st_size,
    })


# ── /api/status/ ──────────────────────────────────────────────────────────────

@require_GET
def api_status(request):
    return JsonResponse({
        "sender": {
            "running": state.sender_state.running,
            "stats": state.sender_state.stats,
        },
        "receiver": {
            "running": state.receiver_state.running,
            "spectrum": state.receiver_state.spectrum,
        },
    })
