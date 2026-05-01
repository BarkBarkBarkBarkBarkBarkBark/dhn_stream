"""
Local UDP probe receiver.

Binds a UDP socket and prints packet rate / byte rate once per second.
Use this to sanity-check the sender before involving DHN-AQ.

Usage:
    dhn-udp-probe --host 0.0.0.0 --port 26090
    dhn-udp-probe --host 0.0.0.0 --port 26090 --expected-bytes 32 --show-samples 4
"""

from __future__ import annotations

import socket
import time
from typing import Annotated

import numpy as np
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

app = typer.Typer(help="Minimal UDP probe receiver for pre-DHN-AQ sanity testing.")
console = Console()


@app.command()
def main(
    host: Annotated[str, typer.Option("--host", help="Bind address")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", help="UDP port to listen on")] = 26090,
    expected_bytes: Annotated[
        int, typer.Option("--expected-bytes", help="Expected payload size in bytes (informational)")
    ] = 0,
    show_samples: Annotated[
        int, typer.Option("--show-samples", help="Decode and print first N int16 values per report interval")
    ] = 0,
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", help="Stop after this many seconds (0 = run until Ctrl-C)")
    ] = 0.0,
) -> None:
    """Bind a UDP socket and report packet/byte rate once per second."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)

    console.print(f"[bold green]Listening on {host}:{port}[/bold green]")
    if expected_bytes > 0:
        console.print(f"  Expected payload size: {expected_bytes} bytes")
    console.print("  Press Ctrl-C to stop.\n")

    packets_total = 0
    bytes_total = 0
    packets_interval = 0
    bytes_interval = 0
    last_payload: bytes | None = None

    t_start = time.perf_counter()
    t_last_report = t_start

    try:
        while True:
            elapsed = time.perf_counter() - t_start
            if timeout_seconds > 0 and elapsed >= timeout_seconds:
                break

            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                data = None

            if data:
                packets_interval += 1
                bytes_interval += len(data)
                packets_total += 1
                bytes_total += len(data)
                last_payload = data

            now = time.perf_counter()
            interval = now - t_last_report
            if interval >= 1.0:
                pps = packets_interval / interval
                bps = bytes_interval / interval
                payload_size = len(last_payload) if last_payload else 0

                table = Table(show_header=True, header_style="bold cyan", box=None)
                table.add_column("Metric", style="dim")
                table.add_column("Value")
                table.add_row("Packets/s", f"{pps:.1f}")
                table.add_row("Bytes/s", f"{bps:,.0f}")
                table.add_row("Last payload (bytes)", str(payload_size))
                table.add_row("Total packets", str(packets_total))
                table.add_row("Total bytes", f"{bytes_total:,}")

                if expected_bytes > 0 and payload_size > 0 and payload_size != expected_bytes:
                    table.add_row(
                        "[red]Size mismatch[/red]",
                        f"got {payload_size}, expected {expected_bytes}",
                    )

                if show_samples > 0 and last_payload:
                    arr = np.frombuffer(last_payload, dtype="<i2")
                    sample_str = ", ".join(str(v) for v in arr[:show_samples])
                    table.add_row(f"First {show_samples} int16", sample_str)

                console.print(table)
                console.print()

                packets_interval = 0
                bytes_interval = 0
                t_last_report = now

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    console.print(f"\n[bold]Done.[/bold] Total packets: {packets_total}, bytes: {bytes_total:,}")
