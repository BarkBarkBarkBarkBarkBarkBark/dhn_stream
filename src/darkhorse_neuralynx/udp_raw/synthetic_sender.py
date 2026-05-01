"""
Synthetic UDP sender using SpikeInterface ground-truth recordings.

Generates a synthetic electrophysiology recording, scales traces to int16,
and sends headerless UDP packets to DHN-AQ (or the local probe receiver).

Usage:
    dhn-si-udp --host 127.0.0.1 --port 26090 --channels 16 --sample-rate 32000 --duration 10
    dhn-si-udp --config configs/synthetic_udp_16ch.yaml
    dhn-si-udp --config configs/synthetic_udp_16ch.yaml --dry-run
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from darkhorse_neuralynx.config.models import AppConfig
from darkhorse_neuralynx.udp_raw.raw_sender import RawUdpSender, SendStats, scale_to_int16

app = typer.Typer(help="Send SpikeInterface synthetic recording as headerless UDP to DHN-AQ.")
console = Console()


def _build_status_table(
    elapsed: float,
    duration: float,
    stats: SendStats,
    channels: int,
    sample_rate_hz: int,
    frames_per_packet: int,
    payload_bytes: int,
) -> Table:
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Metric", style="dim", min_width=26)
    table.add_column("Value")
    table.add_row("Elapsed / Duration", f"{elapsed:.1f}s / {duration:.0f}s")
    table.add_row("Packets sent", f"{stats.packets_sent:,}")
    table.add_row("Bytes sent", f"{stats.bytes_sent:,}")
    table.add_row("Underruns", str(stats.underruns))
    table.add_row("Effective packet rate", f"{stats.effective_packet_rate:.1f} pkt/s")
    table.add_row("Throughput", f"{stats.effective_throughput_mbps:.3f} Mbit/s")
    table.add_row("Channels", str(channels))
    table.add_row("Sample rate", f"{sample_rate_hz} Hz")
    table.add_row("Frames/packet", str(frames_per_packet))
    table.add_row("Payload bytes/packet", str(payload_bytes))
    return table


@app.command()
def main(
    host: Annotated[Optional[str], typer.Option("--host", help="DHN-AQ UDP host")] = None,
    port: Annotated[Optional[int], typer.Option("--port", help="DHN-AQ UDP port")] = None,
    channels: Annotated[Optional[int], typer.Option("--channels", help="Number of channels")] = None,
    sample_rate: Annotated[Optional[int], typer.Option("--sample-rate", help="Sample rate in Hz")] = None,
    duration: Annotated[Optional[float], typer.Option("--duration", help="Duration in seconds")] = None,
    frames_per_packet: Annotated[
        Optional[int], typer.Option("--frames-per-packet", help="Frames per UDP packet")
    ] = None,
    seed: Annotated[Optional[int], typer.Option("--seed", help="Random seed for reproducibility")] = None,
    target_peak: Annotated[
        Optional[int], typer.Option("--target-peak", help="Target peak int16 value after scaling")
    ] = None,
    config: Annotated[
        Optional[Path], typer.Option("--config", help="Path to YAML config file")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print parameters and exit without sending")
    ] = False,
) -> None:
    """Generate SpikeInterface synthetic recording and send as headerless UDP."""

    # Load YAML config as base; CLI flags override individual fields.
    if config is not None:
        cfg = AppConfig.from_yaml(config)
    else:
        cfg = AppConfig()

    if host is not None:
        cfg.udp.host = host
    if port is not None:
        cfg.udp.port = port
    if channels is not None:
        cfg.signal.channels = channels
    if sample_rate is not None:
        cfg.signal.sample_rate_hz = sample_rate
    if duration is not None:
        cfg.signal.duration_seconds = duration
    if frames_per_packet is not None:
        cfg.payload.frames_per_packet = frames_per_packet
    if seed is not None:
        cfg.signal.seed = seed
    if target_peak is not None:
        cfg.payload.target_peak_int16 = target_peak

    n_channels = cfg.signal.channels
    sr = cfg.signal.sample_rate_hz
    dur = cfg.signal.duration_seconds
    fpp = cfg.payload.frames_per_packet
    payload_bytes = fpp * n_channels * 2  # int16 = 2 bytes

    console.print(f"\n[bold]DHN Synthetic UDP Sender[/bold]")
    console.print(f"  Destination:     {cfg.udp.host}:{cfg.udp.port}")
    console.print(f"  Channels:        {n_channels}")
    console.print(f"  Sample rate:     {sr} Hz")
    console.print(f"  Duration:        {dur:.1f} s")
    console.print(f"  Frames/packet:   {fpp}")
    console.print(f"  Payload bytes:   [bold]{payload_bytes}[/bold] bytes/packet")
    console.print(f"  Layout:          {cfg.payload.layout}")
    console.print(f"  Seed:            {cfg.signal.seed}")

    if payload_bytes > 1400:
        console.print(
            f"\n[bold red]WARNING:[/bold red] Payload {payload_bytes} bytes exceeds 1400-byte "
            "Ethernet MTU threshold. UDP fragmentation may occur unless jumbo frames are enabled."
        )

    if dry_run:
        console.print("\n[yellow]Dry-run mode — no packets will be sent.[/yellow]")
        return

    # Import spikeinterface here so the module is importable without it for tests
    try:
        import spikeinterface.core as si
    except ImportError as exc:
        console.print(f"[red]ERROR: spikeinterface not installed: {exc}[/red]")
        raise typer.Exit(1)

    console.print("\n[dim]Generating synthetic recording...[/dim]")
    recording, _ = si.generate_ground_truth_recording(
        durations=[dur],
        sampling_frequency=float(sr),
        num_channels=n_channels,
        num_units=cfg.signal.units,
        seed=cfg.signal.seed,
    )

    total_frames = recording.get_num_frames(segment_index=0)
    chunk_frames = sr  # process 1 second of data per loop iteration

    console.print(
        f"[green]Recording ready:[/green] {total_frames} frames, "
        f"{n_channels} channels, {sr} Hz\n"
    )

    cumulative_stats = SendStats()
    t_run_start = time.perf_counter()

    with RawUdpSender(
        cfg.udp.host,
        cfg.udp.port,
        send_buffer_bytes=cfg.udp.send_buffer_bytes,
        broadcast=cfg.udp.broadcast,
    ) as sender:
        frame_start = 0

        while frame_start < total_frames:
            frame_end = min(frame_start + chunk_frames, total_frames)

            traces = recording.get_traces(
                start_frame=frame_start,
                end_frame=frame_end,
                segment_index=0,
                return_scaled=True,
            )
            traces_int16 = scale_to_int16(traces, target_peak=cfg.payload.target_peak_int16)

            chunk_stats = sender.send_chunk(
                traces_int16,
                sample_rate_hz=sr,
                frames_per_packet=fpp,
                layout=cfg.payload.layout,
            )

            cumulative_stats.packets_sent += chunk_stats.packets_sent
            cumulative_stats.bytes_sent += chunk_stats.bytes_sent
            cumulative_stats.underruns += chunk_stats.underruns
            cumulative_stats.elapsed_seconds = time.perf_counter() - t_run_start

            elapsed = cumulative_stats.elapsed_seconds
            table = _build_status_table(
                elapsed, dur, cumulative_stats, n_channels, sr, fpp, payload_bytes
            )
            console.print(table)
            console.print()

            frame_start = frame_end

    console.print("[bold green]Done.[/bold green]")
    if cumulative_stats.underruns > 0:
        console.print(
            f"[yellow]Note:[/yellow] {cumulative_stats.underruns} pacing underruns — "
            "sender fell behind realtime. Consider reducing channel count or frames_per_packet."
        )
