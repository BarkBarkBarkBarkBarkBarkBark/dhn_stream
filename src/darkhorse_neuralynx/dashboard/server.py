"""No-dependency HTTP dashboard server for live NRD inspection."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from darkhorse_neuralynx.dashboard.app import HTML
from darkhorse_neuralynx.dashboard.labels import (
    ChannelLabel,
    load_channel_config_csv,
    load_connection_map,
    merge_labels,
)
from darkhorse_neuralynx.dashboard.monitor import LiveNrdMonitor
from darkhorse_neuralynx.udp_raw.nrd_file import detect_nrd_file
from darkhorse_neuralynx.udp_raw.nrd_stats import compute_nrd_stats


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def run_dashboard_server(
    *,
    web_host: str,
    web_port: int,
    udp_host: str,
    udp_port: int,
    sample_rate_hz: int,
    expected_channels: int | None = None,
    nrd_file: str = "",
    channel_config: str = "",
    connection_map: str = "",
    waveform_seconds: float = 1.0,
) -> None:
    """Run the local dashboard until interrupted."""
    monitor, file_summary = _build_monitor(
        udp_host=udp_host,
        udp_port=udp_port,
        sample_rate_hz=sample_rate_hz,
        expected_channels=expected_channels,
        nrd_file=nrd_file,
        channel_config=channel_config,
        connection_map=connection_map,
        waveform_seconds=waveform_seconds,
    )

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self._send_html(HTML)
            elif parsed.path == "/api/status":
                max_channels = _query_int(query, "max_channels", None)
                payload = monitor.snapshot(max_channels=max_channels)
                payload["file"] = file_summary
                payload["udp"] = {"host": udp_host, "port": udp_port}
                self._send_json(payload)
            elif parsed.path.startswith("/api/channel/") and parsed.path.endswith("/waveform"):
                channel = _path_channel(parsed.path)
                if channel is None:
                    self._send_json({"error": "invalid channel"}, HTTPStatus.BAD_REQUEST)
                    return
                bins = _query_int(query, "bins", 600) or 600
                self._send_json(monitor.waveform(channel, bins=max(8, min(bins, 2000))).__dict__)
            elif parsed.path == "/api/file-stats":
                if not nrd_file:
                    self._send_json({"error": "no file configured"}, HTTPStatus.BAD_REQUEST)
                    return
                max_packets = _query_int(query, "max_packets", 0) or 0
                report = compute_nrd_stats(
                    Path(nrd_file),
                    sample_rate_hz=sample_rate_hz,
                    max_packets=max_packets or None,
                )
                self._send_json(report.to_dict())
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, body: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server: DashboardHTTPServer | None = None
    try:
        server = DashboardHTTPServer((web_host, web_port), DashboardHandler)
        print(
            f"Dashboard listening at http://{web_host}:{web_port} "
            f"(NRD UDP mirror udp://{udp_host}:{udp_port})",
            flush=True,
        )
        server.serve_forever()
    finally:
        monitor.stop()
        if server is not None:
            server.server_close()


def _build_monitor(
    *,
    udp_host: str,
    udp_port: int,
    sample_rate_hz: int,
    expected_channels: int | None,
    nrd_file: str,
    channel_config: str,
    connection_map: str,
    waveform_seconds: float,
) -> tuple[LiveNrdMonitor, dict[str, Any]]:
    n_channels = expected_channels
    file_summary: dict[str, Any] = {}
    if nrd_file:
        layout = detect_nrd_file(nrd_file)
        n_channels = layout.n_channels
        file_summary = {
            "path": str(layout.path),
            "file_size": layout.file_size,
            "header_bytes": layout.header_bytes,
            "packet_size": layout.packet_size,
            "n_channels": layout.n_channels,
            "packet_count": layout.packet_count,
        }

    label_sources: list[dict[int, ChannelLabel]] = []
    if channel_config:
        label_sources.append(load_channel_config_csv(channel_config))
    if connection_map:
        label_sources.append(load_connection_map(connection_map))
    labels = merge_labels(n_channels, *label_sources) if n_channels else []

    monitor = LiveNrdMonitor(
        expected_channels=n_channels,
        sample_rate_hz=sample_rate_hz,
        waveform_seconds=waveform_seconds,
        labels=labels,
    )
    monitor.start_udp_listener(udp_host, udp_port)
    return monitor, file_summary


def _query_int(query: dict[str, list[str]], key: str, default: int | None) -> int | None:
    value = query.get(key, [None])[0]
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _path_channel(path: str) -> int | None:
    parts = path.strip("/").split("/")
    if len(parts) != 4:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None
