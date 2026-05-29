"""
dhn-stream — Neuralynx NRD UDP streamer for DHN_Acq.

Emits real Neuralynx NRD wire-format packets (one UDP datagram per sample
timestep, all channels as int32) so DHN_Acq decodes the stream as if it
came from real Atlas/Pegasus hardware.

Two signal sources:
    --source harmonic        multi-channel harmonic sine fingerprint
    --source spikeinterface  synthetic ground-truth recording + white noise

See docs/nrd-format.md for the wire-format reference.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.console import Group

from darkhorse_neuralynx.cli_helpers import build_harmonic_chunk, status_table
from darkhorse_neuralynx.udp_raw.nrd_file import detect_nrd_file, iter_nrd_samples
from darkhorse_neuralynx.udp_raw.nrd_sender import NrdUdpSender, nrd_packet_size_bytes
from darkhorse_neuralynx.udp_raw.nrd_stats import compute_nrd_stats

app = typer.Typer(
    name="dhn-stream",
    help=(
        "DHN Neuralynx NRD UDP streamer for DHN_Acq.\n\n"
        "  [cyan]dhn-stream stream[/cyan]   publish NRD packets over UDP"
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()

DEFAULT_DASHBOARD_FILE = "/mnt/MED_Data_iSSD/DA-075-RawData.nrd"
DEFAULT_DASHBOARD_CHANNEL_CONFIG = "/home/dhn/DHN/DHN_Acq/DHN_Acq_cs.csv"
DEFAULT_DASHBOARD_CONNECTION_MAP = "/mnt/MED_Data_iSSD/DA075_connection_map.xlsx"


def _default_existing_path(path: str) -> str:
    """Return a local default path only when it exists on this machine."""
    return path if Path(path).exists() else ""


def _start_enter_listener() -> threading.Event:
    """Return an event that is set once stdin receives Enter or EOF."""
    event = threading.Event()

    def wait_for_enter() -> None:
        try:
            sys.stdin.readline()
        finally:
            event.set()

    thread = threading.Thread(target=wait_for_enter, daemon=True)
    thread.start()
    return event


@app.command(hidden=True)
def version() -> None:
    """Print package version."""
    console.print("dhn-stream 0.1.0")


def _startup_panel(
    host: str,
    port: int,
    channels: int,
    sample_rate: int,
    source: str,
    fundamental: float,
) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="bold cyan", min_width=22)
    table.add_column("v", style="white")

    pkt_sz = nrd_packet_size_bytes(channels)
    table.add_row("Streaming to", f"[bold green]udp://{host}:{port}[/bold green]")
    table.add_row("Format", "[bold green]Neuralynx NRD[/bold green] (1 pkt/timestep, int32)")
    source_label_map = {
        "spikeinterface": "SpikeInterface spikes + noise",
        "nrd-file": "replay from .nrd file",
        "harmonic": "harmonic sine",
    }
    table.add_row("Source", source_label_map.get(source, source))
    table.add_row("Channels", str(channels))
    if source == "harmonic":
        table.add_row("Fundamental", f"{fundamental} Hz")
    table.add_row("Sample rate", f"{sample_rate} Hz")
    table.add_row("Packet size", f"{pkt_sz} bytes")

    tip_lines = [
        "",
        "[bold]Common reconfigurations:[/bold]",
        "",
        "  [cyan]dhn-stream stream --host 192.168.3.50[/cyan]            send to a remote DHN_Acq box",
        "  [cyan]dhn-stream stream --channels 16 --sample-rate 32000[/cyan]",
        "  [cyan]dhn-stream stream --source spikeinterface --units 8[/cyan]  noisy spike source",
        "  [cyan]dhn-stream stream --source nrd-file --file /path/to.nrd[/cyan]  replay a real .nrd recording",
        "  [cyan]dhn-stream stream --duration 60[/cyan]                  run for 60 s then stop",
        "  [cyan]dhn-stream stream --dry-run[/cyan]                      print params, no packets",
        "",
        "  [cyan]dhn-stream stream --help[/cyan]                         show all options",
        "",
        "[dim]Press Ctrl-C to stop.[/dim]",
    ]
    return Panel(
        Group(table, Padding(Text.from_markup("\n".join(tip_lines)), (1, 0, 0, 0))),
        title="[bold cyan]DHN Stream[/bold cyan]",
        border_style="cyan",
        expand=False,
    )


@app.command()
def stream(
    host: Annotated[str, typer.Option("--host", "-H", help="Destination UDP host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Destination UDP port")] = 26090,
    channels: Annotated[int, typer.Option("--channels", "-c", help="Number of channels")] = 4,
    sample_rate: Annotated[int, typer.Option("--sample-rate", "-r", help="Sample rate in Hz")] = 32000,
    source: Annotated[Literal["harmonic", "spikeinterface", "nrd-file"], typer.Option("--source", help="Signal source")] = "harmonic",
    nrd_file: Annotated[str, typer.Option("--file", help="Path to a .nrd file (required for --source nrd-file)")] = "",
    fundamental: Annotated[float, typer.Option("--fundamental", "-f", help="Fundamental frequency (harmonic source)")] = 440.0,
    units: Annotated[int, typer.Option("--units", help="SpikeInterface units (spike source only)")] = 8,
    seed: Annotated[int, typer.Option("--seed", help="SpikeInterface/noise random seed")] = 42,
    recording_seconds: Annotated[float, typer.Option("--recording-seconds", help="Generated recording length before looping")] = 30.0,
    noise_std: Annotated[float, typer.Option("--noise-std", help="White noise std in ADC counts (spike source)")] = 5_000.0,
    target_peak: Annotated[int, typer.Option("--peak", help="Target peak amplitude in NRD ADC counts (~24-bit FS = 8_388_608). Spike: spike peak. Harmonic: sine peak.")] = 100_000,
    send_buffer: Annotated[int, typer.Option("--send-buffer", help="UDP socket send buffer bytes")] = 8_388_608,
    mirror_host: Annotated[str, typer.Option("--mirror-host", help="Optional UDP mirror host for dashboard/probe inspection")] = "",
    mirror_port: Annotated[int, typer.Option("--mirror-port", help="UDP mirror destination port")] = 26091,
    dashboard_mirror: Annotated[bool, typer.Option("--dashboard-mirror", help="Mirror packets to the local dashboard at 127.0.0.1:26091")] = False,
    duration: Annotated[float, typer.Option("--duration", "-d", help="Run duration in seconds (0 = infinite)")] = 0.0,
    prime_until_enter: Annotated[
        bool,
        typer.Option(
            "--prime-until-enter",
            help="For --source nrd-file, send zero-valued packets until Enter is pressed, then replay the file",
        ),
    ] = False,
    chunk_seconds: Annotated[float, typer.Option("--chunk", help="Seconds per loop iteration", hidden=True)] = 0.05,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print parameters and exit without sending")] = False,
) -> None:
    """Publish a multi-channel NRD UDP stream to DHN_Acq."""
    if prime_until_enter and source != "nrd-file":
        console.print("[red]--prime-until-enter is only supported with --source nrd-file[/red]")
        raise typer.Exit(2)

    nrd_layout = None
    if source == "nrd-file":
        if not nrd_file:
            console.print("[red]--source nrd-file requires --file PATH[/red]")
            raise typer.Exit(2)
        try:
            nrd_layout = detect_nrd_file(nrd_file)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]Could not read NRD file: {exc}[/red]")
            raise typer.Exit(1)
        # Channel count is dictated by the file. Warn if user passed a different value.
        cli_default_channels = 4
        if channels != cli_default_channels and channels != nrd_layout.n_channels:
            console.print(
                f"[yellow]--channels {channels} ignored; .nrd file has "
                f"{nrd_layout.n_channels} channels.[/yellow]"
            )
        channels = nrd_layout.n_channels

    console.print()
    console.print(_startup_panel(host, port, channels, sample_rate, source, fundamental))

    if nrd_layout is not None:
        approx_seconds = nrd_layout.packet_count / float(sample_rate)
        console.print(
            f"[dim]NRD file: {nrd_layout.path}\n"
            f"  size={nrd_layout.file_size / 1e9:.2f} GB, "
            f"header={nrd_layout.header_bytes} B, packet={nrd_layout.packet_size} B, "
            f"channels={nrd_layout.n_channels}, packets={nrd_layout.packet_count:,}\n"
            f"  one-shot replay, ~{approx_seconds:.1f} s at {sample_rate} Hz[/dim]"
        )

    if dry_run:
        console.print("[yellow]Dry-run — no packets sent.[/yellow]\n")
        raise typer.Exit()

    chunk_frames = max(1, int(sample_rate * chunk_seconds))
    payload_bytes = nrd_packet_size_bytes(channels)
    i32_peak = max(1, int(target_peak))

    console.print(Rule(style="cyan"))
    console.print()

    total_packets = 0
    total_bytes = 0
    total_underruns = 0
    if dashboard_mirror and not mirror_host:
        mirror_host = "127.0.0.1"
    mirror_targets = ((mirror_host, mirror_port),) if mirror_host else ()
    mirror_packets = 0
    mirror_bytes = 0
    t_start = time.perf_counter()
    t_offset = 0.0
    source_label_map = {
        "spikeinterface": "SpikeInterface spikes + noise",
        "nrd-file": f".nrd replay ({Path(nrd_file).name})" if nrd_file else ".nrd replay",
        "harmonic": "harmonic sine",
    }
    source_label = source_label_map[source]

    spike_recording = None
    spike_total_frames = 0
    spike_scale = 1.0
    spike_frame_cursor = 0
    noise_rng = np.random.default_rng(seed)
    nrd_iter = None
    if source == "nrd-file" and not prime_until_enter:
        assert nrd_layout is not None
        nrd_iter = iter_nrd_samples(nrd_layout, batch_packets=chunk_frames)

    if source == "spikeinterface":
        try:
            import spikeinterface.core as si
        except ImportError as exc:
            console.print(f"[red]SpikeInterface source requires spikeinterface: {exc}[/red]")
            raise typer.Exit(1)

        console.print("[dim]Generating SpikeInterface recording...[/dim]")
        spike_recording, _sorting = si.generate_ground_truth_recording(
            durations=[max(recording_seconds, chunk_seconds)],
            sampling_frequency=float(sample_rate),
            num_channels=channels,
            num_units=units,
            seed=seed,
        )
        spike_total_frames = spike_recording.get_num_frames(segment_index=0)
        estimate_frames = min(spike_total_frames, max(chunk_frames, sample_rate))
        estimate = spike_recording.get_traces(
            start_frame=0,
            end_frame=estimate_frames,
            segment_index=0,
            return_scaled=True,
        ).astype(np.float64)
        # 99.99th percentile so --peak maps to the spike-peak amplitude in counts.
        # Lower percentiles are dominated by noise floor in sparse-spiking data and
        # under-scale the spikes, making traces look flat on DHN_Acq's ~24-bit display.
        spike_peak = float(np.percentile(np.abs(estimate), 99.99)) or 1.0
        spike_scale = i32_peak / spike_peak
        console.print(
            f"[green]SpikeInterface ready:[/green] {spike_total_frames:,} frames, "
            f"{channels} channels, {units} units, scale={spike_scale:.3g}, "
            f"noise_std={noise_std:g} counts\n"
        )

    try:
        replay_finished = False
        primer_packets = 0
        primer_bytes = 0
        with NrdUdpSender(
            host,
            port,
            send_buffer_bytes=send_buffer,
            mirror_targets=mirror_targets,
        ) as sender:
            sender.reset_pacing()
            t_start = time.perf_counter()
            target_frames = int(round(duration * sample_rate)) if duration > 0 else 0
            frames_sent = 0

            with Live(console=console, refresh_per_second=4) as live:
                if prime_until_enter:
                    assert nrd_layout is not None
                    console.print(
                        "[bold yellow]Priming with zero-valued packets.[/bold yellow] "
                        "Start DHN_Acq recording, then press Enter here to replay the .nrd file."
                    )
                    enter_pressed = _start_enter_listener()
                    zero_chunk = np.zeros((chunk_frames, channels), dtype="<i4")
                    while not enter_pressed.is_set():
                        stats = sender.send_chunk(zero_chunk, sample_rate_hz=sample_rate)
                        total_packets += stats.packets_sent
                        total_bytes += stats.bytes_sent
                        total_underruns += stats.underruns
                        mirror_packets += stats.mirror_packets_sent
                        mirror_bytes += stats.mirror_bytes_sent
                        primer_packets += stats.packets_sent
                        primer_bytes += stats.bytes_sent
                        elapsed = time.perf_counter() - t_start
                        live.update(status_table(
                            elapsed, 0.0,
                            stats.packets_sent, stats.bytes_sent, stats.underruns,
                            total_packets, total_bytes, total_underruns,
                            channels, sample_rate, fundamental,
                            payload_bytes, "zero primer (press Enter to replay)",
                        ))
                    nrd_iter = iter_nrd_samples(nrd_layout, batch_packets=chunk_frames)

                while True:
                    if duration > 0 and frames_sent >= target_frames:
                        break
                    frames_this_chunk = chunk_frames
                    if duration > 0:
                        frames_this_chunk = min(chunk_frames, target_frames - frames_sent)

                    if source == "spikeinterface":
                        assert spike_recording is not None
                        frame_end = spike_frame_cursor + frames_this_chunk
                        if frame_end <= spike_total_frames:
                            traces = spike_recording.get_traces(
                                start_frame=spike_frame_cursor,
                                end_frame=frame_end,
                                segment_index=0,
                                return_scaled=True,
                            )
                        else:
                            first = spike_recording.get_traces(
                                start_frame=spike_frame_cursor,
                                end_frame=spike_total_frames,
                                segment_index=0,
                                return_scaled=True,
                            )
                            second = spike_recording.get_traces(
                                start_frame=0,
                                end_frame=frame_end % spike_total_frames,
                                segment_index=0,
                                return_scaled=True,
                            )
                            traces = np.vstack([first, second])
                        spike_frame_cursor = frame_end % spike_total_frames
                        traces = traces.astype(np.float64) * spike_scale
                        if noise_std > 0:
                            traces += noise_rng.normal(0.0, noise_std, size=traces.shape)
                        chunk_i32 = np.clip(traces, -2_147_483_647, 2_147_483_647).astype("<i4")
                    elif source == "nrd-file":
                        try:
                            chunk_i32 = next(nrd_iter)  # type: ignore[arg-type]
                        except StopIteration:
                            replay_finished = True
                            break
                        if duration > 0 and chunk_i32.shape[0] > frames_this_chunk:
                            chunk_i32 = chunk_i32[:frames_this_chunk]
                        frames_this_chunk = chunk_i32.shape[0]
                    else:
                        chunk = build_harmonic_chunk(frames_this_chunk, channels, sample_rate, fundamental, t_offset)
                        chunk_i32 = np.clip(chunk * i32_peak, -2_147_483_647, 2_147_483_647).astype("<i4")

                    stats = sender.send_chunk(chunk_i32, sample_rate_hz=sample_rate)
                    total_packets += stats.packets_sent
                    total_bytes += stats.bytes_sent
                    total_underruns += stats.underruns
                    mirror_packets += stats.mirror_packets_sent
                    mirror_bytes += stats.mirror_bytes_sent
                    frames_sent += stats.packets_sent
                    t_offset += frames_this_chunk / sample_rate
                    elapsed = time.perf_counter() - t_start

                    display_duration = duration
                    if source == "nrd-file" and nrd_layout is not None and duration <= 0:
                        display_duration = nrd_layout.packet_count / float(sample_rate)

                    live.update(status_table(
                        elapsed, display_duration,
                        stats.packets_sent, stats.bytes_sent, stats.underruns,
                        total_packets, total_bytes, total_underruns,
                        channels, sample_rate, fundamental,
                        payload_bytes, source_label,
                    ))
    except KeyboardInterrupt:
        pass

    elapsed = time.perf_counter() - t_start
    done_label = "Yayy!! Replay Done!" if replay_finished else "Done."
    primer_summary = ""
    if primer_packets:
        primer_summary = (
            f"\nPrimer sent [bold]{primer_packets:,}[/bold] packets · "
            f"[bold]{primer_bytes / 1e6:.2f}[/bold] MB"
        )
    mirror_summary = ""
    if mirror_targets:
        mirror_summary = (
            f"\nMirrored [bold]{mirror_packets:,}[/bold] packets · "
            f"[bold]{mirror_bytes / 1e6:.2f}[/bold] MB to udp://{mirror_host}:{mirror_port}"
        )
    console.print(
        f"\n[bold green]{done_label}[/bold green]  "
        f"Sent [bold]{total_packets:,}[/bold] packets · "
        f"[bold]{total_bytes / 1e6:.2f}[/bold] MB · "
        f"elapsed {elapsed:.1f} s"
        f"{primer_summary}\n"
        f"{mirror_summary}\n"
    )


@app.command()
def dashboard(
    web_host: Annotated[str, typer.Option("--web-host", help="Dashboard web server host")] = "127.0.0.1",
    web_port: Annotated[int, typer.Option("--web-port", help="Dashboard web server port")] = 8000,
    udp_host: Annotated[str, typer.Option("--udp-host", help="UDP host/interface for mirrored NRD packets")] = "127.0.0.1",
    udp_port: Annotated[int, typer.Option("--udp-port", help="UDP port for mirrored NRD packets")] = 26091,
    sample_rate: Annotated[int, typer.Option("--sample-rate", "-r", help="Sample rate in Hz")] = 32_000,
    channels: Annotated[int, typer.Option("--channels", "-c", help="Expected channels (0 = infer from file or first packet)")] = 0,
    nrd_file: Annotated[str, typer.Option("--file", help="Optional .nrd file for layout/file stats")] = DEFAULT_DASHBOARD_FILE,
    channel_config: Annotated[
        str,
        typer.Option("--channel-config", help="Optional DHN_Acq_cs.csv/tsv for labels"),
    ] = DEFAULT_DASHBOARD_CHANNEL_CONFIG,
    connection_map: Annotated[
        str,
        typer.Option("--connection-map", help="Optional CSV/TSV/XLSX connection map for labels"),
    ] = DEFAULT_DASHBOARD_CONNECTION_MAP,
    waveform_seconds: Annotated[float, typer.Option("--waveform-seconds", help="Seconds kept in waveform ring buffer")] = 1.0,
) -> None:
    """Launch the optional local waveform inspection dashboard."""
    try:
        from darkhorse_neuralynx.dashboard.server import run_dashboard_server
    except ImportError as exc:
        console.print(f"[red]Dashboard import failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    expected_channels = channels if channels > 0 else None
    nrd_file = nrd_file if nrd_file != DEFAULT_DASHBOARD_FILE else _default_existing_path(nrd_file)
    channel_config = (
        channel_config
        if channel_config != DEFAULT_DASHBOARD_CHANNEL_CONFIG
        else _default_existing_path(channel_config)
    )
    connection_map = (
        connection_map
        if connection_map != DEFAULT_DASHBOARD_CONNECTION_MAP
        else _default_existing_path(connection_map)
    )
    try:
        run_dashboard_server(
            web_host=web_host,
            web_port=web_port,
            udp_host=udp_host,
            udp_port=udp_port,
            sample_rate_hz=sample_rate,
            expected_channels=expected_channels,
            nrd_file=nrd_file,
            channel_config=channel_config,
            connection_map=connection_map,
            waveform_seconds=waveform_seconds,
        )
    except (FileNotFoundError, ValueError, ImportError, OSError) as exc:
        console.print(f"[red]Could not start dashboard: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def stats(
    nrd_file: Annotated[str, typer.Option("--file", help="Path to a .nrd file to analyze")],
    sample_rate: Annotated[
        int,
        typer.Option(
            "--sample-rate",
            "-r",
            help="Sample rate in Hz for duration reporting (0 = unknown)",
        ),
    ] = 0,
    max_packets: Annotated[
        int,
        typer.Option("--max-packets", help="Analyze at most this many packets (0 = full file)"),
    ] = 0,
    seconds: Annotated[
        float,
        typer.Option("--seconds", help="Analyze only this many seconds from the start (requires --sample-rate)"),
    ] = 0.0,
    batch_packets: Annotated[int, typer.Option("--batch-packets", help="Packets read per batch", hidden=True)] = 4096,
    flat_peak_threshold: Annotated[
        int,
        typer.Option("--flat-peak-threshold", help="Channels with max abs <= this are marked flat"),
    ] = 0,
    flat_std_threshold: Annotated[
        float,
        typer.Option("--flat-std-threshold", help="Channels with std <= this are marked flat"),
    ] = 1.0,
    noise_peak_to_rms_threshold: Annotated[
        float,
        typer.Option(
            "--noise-peak-to-rms-threshold",
            help="Peak/RMS at or below this is marked noise-like",
        ),
    ] = 8.0,
    max_rows: Annotated[int, typer.Option("--max-rows", help="Maximum channel rows to print (0 = all)")] = 64,
    report_json: Annotated[str, typer.Option("--report-json", help="Write full statistics as JSON to this path")] = "",
) -> None:
    """Report streaming per-channel statistics for a Neuralynx NRD file."""
    if seconds > 0 and sample_rate <= 0:
        console.print("[red]--seconds requires --sample-rate[/red]")
        raise typer.Exit(2)
    if seconds > 0 and max_packets > 0:
        console.print("[red]Use either --seconds or --max-packets, not both[/red]")
        raise typer.Exit(2)

    packets_limit = max_packets if max_packets > 0 else None
    if seconds > 0:
        packets_limit = max(1, int(round(seconds * sample_rate)))

    try:
        report = compute_nrd_stats(
            nrd_file,
            sample_rate_hz=sample_rate if sample_rate > 0 else None,
            max_packets=packets_limit,
            batch_packets=batch_packets,
            flat_peak_threshold=flat_peak_threshold,
            flat_std_threshold=flat_std_threshold,
            noise_peak_to_rms_threshold=noise_peak_to_rms_threshold,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Could not analyze NRD file: {exc}[/red]")
        raise typer.Exit(1)

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("k", style="bold cyan", min_width=22)
    summary.add_column("v", style="white")
    summary.add_row("NRD file", report.path)
    summary.add_row("Channels", str(report.n_channels))
    summary.add_row("Packet size", f"{report.packet_size} bytes")
    summary.add_row("Packets analyzed", f"{report.packets_analyzed:,} / {report.packets_in_file:,}")
    if report.estimated_duration_seconds is not None:
        summary.add_row("Analyzed duration", f"{report.estimated_duration_seconds:.3f} s")
    header_hints = []
    if report.header_mentions_micro:
        header_hints.append("micro")
    if report.header_mentions_macro:
        header_hints.append("macro")
    summary.add_row("Header hints", ", ".join(header_hints) if header_hints else "none")
    console.print(Panel(summary, title="[bold cyan]NRD Statistics[/bold cyan]", border_style="cyan", expand=False))

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Ch", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Mean", justify="right")
    table.add_column("Std", justify="right")
    table.add_column("RMS", justify="right")
    table.add_column("MaxAbs", justify="right")
    table.add_column("Pk/RMS", justify="right")
    table.add_column("Quality")
    table.add_column("Electrode")

    rows = report.channels if max_rows <= 0 else report.channels[:max_rows]
    for channel in rows:
        table.add_row(
            str(channel.channel),
            str(channel.minimum),
            str(channel.maximum),
            f"{channel.mean:.2f}",
            f"{channel.std:.2f}",
            f"{channel.rms:.2f}",
            str(channel.max_abs),
            f"{channel.peak_to_rms:.2f}",
            channel.quality,
            channel.electrode_type,
        )
    console.print(table)
    if max_rows > 0 and report.n_channels > max_rows:
        console.print(
            f"[dim]Showing {max_rows} of {report.n_channels} channels. "
            "Use --max-rows 0 to print all.[/dim]"
        )

    if report_json:
        output_path = Path(report_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Wrote JSON report:[/green] {output_path}")
