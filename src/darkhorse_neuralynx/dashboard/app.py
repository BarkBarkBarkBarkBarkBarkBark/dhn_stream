"""FastAPI app factory for the optional local dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from darkhorse_neuralynx.dashboard.labels import (
    ChannelLabel,
    load_channel_config_csv,
    load_connection_map,
    merge_labels,
)
from darkhorse_neuralynx.dashboard.monitor import LiveNrdMonitor
from darkhorse_neuralynx.udp_raw.nrd_file import detect_nrd_file
from darkhorse_neuralynx.udp_raw.nrd_stats import compute_nrd_stats


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DHN Waveform Inspector</title>
  <style>
    :root { color-scheme: dark; --bg: #101114; --panel: #181b20; --line: #2c313a; --text: #eceff4; --muted: #9aa4b2; --good: #5fd38d; --warn: #f0c95a; --bad: #ef6f6c; --accent: #75b7ff; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); }
    header { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--line); background: #13161a; position: sticky; top: 0; z-index: 3; }
    h1 { font-size: 18px; margin: 0; font-weight: 700; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .top { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
    .pill { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; background: var(--panel); font-size: 12px; white-space: nowrap; }
    main { display: grid; grid-template-columns: minmax(560px, 1fr) minmax(360px, 560px); gap: 14px; padding: 14px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; min-width: 0; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line); }
    .section-head h2 { font-size: 14px; margin: 0; }
    .controls { display: flex; gap: 8px; align-items: center; }
    input, select, button { background: #111419; color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; font: inherit; font-size: 12px; }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    .table-wrap { max-height: calc(100vh - 156px); overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th { position: sticky; top: 0; background: #20242b; color: var(--muted); text-align: right; padding: 7px 8px; border-bottom: 1px solid var(--line); }
    td { padding: 6px 8px; border-bottom: 1px solid #242932; text-align: right; font-variant-numeric: tabular-nums; }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
    tr { cursor: pointer; }
    tr:hover { background: #202832; }
    tr.selected { background: #223349; }
    .quality { font-weight: 700; text-align: left; }
    .flat { color: var(--bad); }
    .noise-like { color: var(--warn); }
    .signal-like { color: var(--good); }
    .no-data { color: var(--muted); }
    #waveform { width: 100%; height: 260px; display: block; background: #0d0f12; border-bottom: 1px solid var(--line); }
    .detail { padding: 12px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; font-size: 12px; }
    .metric { display: flex; justify-content: space-between; gap: 10px; border-bottom: 1px solid #252a32; padding-bottom: 5px; }
    .metric span:first-child { color: var(--muted); }
    .empty { color: var(--muted); padding: 20px; }
    @media (max-width: 980px) { main { grid-template-columns: 1fr; } .table-wrap { max-height: 58vh; } }
  </style>
</head>
<body>
  <header>
    <div><h1>DHN Waveform Inspector</h1><div class="sub" id="source">waiting for packets</div></div>
    <div class="top" id="status"></div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>Channels</h2>
        <div class="controls">
          <select id="filter"><option value="all">all</option><option value="flat">flat</option><option value="noise-like">noise-like</option><option value="signal-like">signal-like</option></select>
          <input id="search" placeholder="channel or label" size="16">
          <button id="pause">pause</button>
        </div>
      </div>
      <div class="table-wrap"><table><thead><tr><th>Ch</th><th>Label</th><th>RMS</th><th>MaxAbs</th><th>Pk/RMS</th><th>Quality</th></tr></thead><tbody id="channels"></tbody></table></div>
    </section>
    <section>
      <div class="section-head"><h2 id="detailTitle">Channel detail</h2><div class="controls"><button id="refreshWave">refresh waveform</button></div></div>
      <canvas id="waveform" width="900" height="260"></canvas>
      <div class="detail" id="detail"><div class="empty">Select a channel to inspect waveform and metrics.</div></div>
    </section>
  </main>
<script>
let selected = 1;
let paused = false;
let latest = null;
const fmt = (v, d=1) => v === null || v === undefined ? "--" : Number(v).toFixed(d);
const intfmt = (v) => v === null || v === undefined ? "--" : Math.round(Number(v)).toLocaleString();
function pill(k, v) { return `<div class="pill"><b>${k}</b> ${v}</div>`; }
function renderStatus(data) {
  document.getElementById("source").textContent = `${data.n_channels || 0} channels · ${data.sample_rate_hz || "?"} Hz`;
  document.getElementById("status").innerHTML = [
    pill("pkt/s", fmt(data.packet_rate_hz, 0)),
    pill("Mbit/s", fmt(data.throughput_mbps, 2)),
    pill("packets", intfmt(data.packet_count)),
    pill("size", `${data.last_packet_size || 0} B`),
    pill("errors", intfmt((data.parse_errors || 0) + (data.channel_mismatch_errors || 0))),
    pill("gaps", intfmt((data.packet_id_gaps || 0) + (data.timestamp_gaps || 0))),
    pill("age", data.last_packet_age_seconds === null ? "--" : `${fmt(data.last_packet_age_seconds, 2)}s`),
  ].join("");
}
function renderChannels(data) {
  const tbody = document.getElementById("channels");
  const filter = document.getElementById("filter").value;
  const query = document.getElementById("search").value.toLowerCase();
  const rows = data.channels.filter(ch => {
    const label = (ch.label?.name || "").toLowerCase();
    const matchesFilter = filter === "all" || ch.quality === filter;
    const matchesQuery = !query || String(ch.channel) === query || label.includes(query);
    return matchesFilter && matchesQuery;
  });
  tbody.innerHTML = rows.map(ch => `<tr class="${ch.channel === selected ? "selected" : ""}" data-ch="${ch.channel}">
    <td>${ch.channel}</td><td>${ch.label?.name || ""}</td><td>${intfmt(ch.rms)}</td><td>${intfmt(ch.max_abs)}</td><td>${fmt(ch.peak_to_rms, 2)}</td><td class="quality ${ch.quality}">${ch.quality}</td>
  </tr>`).join("");
  tbody.querySelectorAll("tr").forEach(row => row.addEventListener("click", () => { selected = Number(row.dataset.ch); renderChannels(latest); renderDetail(); }));
}
function renderDetail() {
  if (!latest) return;
  const ch = latest.channels.find(row => row.channel === selected);
  if (!ch) return;
  document.getElementById("detailTitle").textContent = `Channel ${selected} · ${ch.label?.name || ""}`;
  document.getElementById("detail").innerHTML = [
    ["quality", ch.quality], ["type", ch.label?.electrode_type || "unknown"], ["description", ch.label?.description || ""], ["count", intfmt(ch.count)],
    ["min", intfmt(ch.minimum)], ["max", intfmt(ch.maximum)], ["mean", fmt(ch.mean, 2)], ["std", fmt(ch.std, 2)],
    ["rms", fmt(ch.rms, 2)], ["max abs", intfmt(ch.max_abs)], ["peak/rms", fmt(ch.peak_to_rms, 2)], ["source", ch.label?.source || ""],
  ].map(([k,v]) => `<div class="metric"><span>${k}</span><span>${v || "--"}</span></div>`).join("");
  fetchWaveform();
}
function drawWaveform(wave) {
  const canvas = document.getElementById("waveform");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0d0f12"; ctx.fillRect(0, 0, w, h);
  const mins = wave.min_values || [], maxs = wave.max_values || [];
  if (!mins.length) { ctx.fillStyle = "#9aa4b2"; ctx.fillText("no waveform samples yet", 20, 30); return; }
  let lo = Math.min(...mins), hi = Math.max(...maxs);
  if (lo === hi) { lo -= 1; hi += 1; }
  const y = v => h - 18 - ((v - lo) / (hi - lo)) * (h - 36);
  ctx.strokeStyle = "#2c313a"; ctx.beginPath(); ctx.moveTo(0, y(0)); ctx.lineTo(w, y(0)); ctx.stroke();
  ctx.strokeStyle = "#75b7ff"; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < mins.length; i++) {
    const x = (i / Math.max(1, mins.length - 1)) * w;
    ctx.moveTo(x, y(mins[i])); ctx.lineTo(x, y(maxs[i]));
  }
  ctx.stroke();
  ctx.fillStyle = "#9aa4b2"; ctx.fillText(`${intfmt(lo)} to ${intfmt(hi)} counts · ${wave.samples_seen} samples`, 12, 18);
}
async function fetchWaveform() {
  const response = await fetch(`/api/channel/${selected}/waveform?bins=700`);
  drawWaveform(await response.json());
}
async function tick() {
  if (!paused) {
    const response = await fetch("/api/status?max_channels=512");
    latest = await response.json();
    renderStatus(latest); renderChannels(latest); renderDetail();
  }
  setTimeout(tick, 1000);
}
document.getElementById("pause").addEventListener("click", ev => { paused = !paused; ev.target.textContent = paused ? "resume" : "pause"; });
document.getElementById("refreshWave").addEventListener("click", fetchWaveform);
document.getElementById("filter").addEventListener("change", () => latest && renderChannels(latest));
document.getElementById("search").addEventListener("input", () => latest && renderChannels(latest));
tick();
</script>
</body>
</html>
"""


def create_dashboard_app(
    *,
    udp_host: str,
    udp_port: int,
    sample_rate_hz: int,
    expected_channels: int | None = None,
    nrd_file: str = "",
    channel_config: str = "",
    connection_map: str = "",
    waveform_seconds: float = 1.0,
) -> Any:
    """Create and start the optional FastAPI dashboard app."""
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import HTMLResponse
    except ImportError as exc:
        raise ImportError("Dashboard requires optional dependencies: fastapi and uvicorn") from exc

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

    labels: list[ChannelLabel] = []
    label_sources: list[dict[int, ChannelLabel]] = []
    if channel_config:
        label_sources.append(load_channel_config_csv(channel_config))
    if connection_map:
        label_sources.append(load_connection_map(connection_map))
    if n_channels:
        labels = merge_labels(n_channels, *label_sources)

    monitor = LiveNrdMonitor(
        expected_channels=n_channels,
        sample_rate_hz=sample_rate_hz,
        waveform_seconds=waveform_seconds,
        labels=labels,
    )
    monitor.start_udp_listener(udp_host, udp_port)

    app = FastAPI(title="DHN Waveform Inspector")
    app.state.monitor = monitor

    @app.on_event("shutdown")
    def shutdown() -> None:
        monitor.stop()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML

    @app.get("/api/status")
    def status(max_channels: int | None = Query(default=None, ge=1, le=2048)) -> dict[str, Any]:
        payload = monitor.snapshot(max_channels=max_channels)
        payload["file"] = file_summary
        payload["udp"] = {"host": udp_host, "port": udp_port}
        return payload

    @app.get("/api/channel/{channel}/waveform")
    def waveform(channel: int, bins: int = Query(default=600, ge=8, le=2000)) -> dict[str, Any]:
        return monitor.waveform(channel, bins=bins).__dict__

    @app.get("/api/file-stats")
    def file_stats(max_packets: int = Query(default=0, ge=0)) -> dict[str, Any]:
        if not nrd_file:
            return {"error": "no file configured"}
        report = compute_nrd_stats(
            Path(nrd_file),
            sample_rate_hz=sample_rate_hz,
            max_packets=max_packets or None,
        )
        return report.to_dict()

    return app
