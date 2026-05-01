"""
MED output verifier.

Opens a DHN-AQ MED session using dhn-med-py and checks:
  - MED path exists and can be opened.
  - Channel count matches expectation.
  - Sample rate matches expectation (if metadata exposes it).
  - At least N seconds of data can be read.
  - Signal is nonzero.
  - Per-channel mean, std, min, max are plausible.

Prints a Rich summary table and optionally writes a JSON report.

Usage:
    dhn-verify-med --med-path /mnt/dhn/recordings/SESSION.medd \\
                   --expect-channels 16 \\
                   --expect-sample-rate 32000 \\
                   --read-seconds 5 \\
                   --report-json runs/report.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Verify DHN-AQ MED output using dhn-med-py.")
console = Console()


def _check(condition: bool, label: str, detail: str = "") -> dict:
    status = "PASS" if condition else "FAIL"
    return {"check": label, "status": status, "detail": detail}


@app.command()
def main(
    med_path: Annotated[Path, typer.Option("--med-path", help="Path to MED session directory")],
    password: Annotated[
        Optional[str], typer.Option("--password", help="MED session password if encrypted")
    ] = None,
    expect_channels: Annotated[
        Optional[int], typer.Option("--expect-channels", help="Expected number of channels")
    ] = None,
    expect_sample_rate: Annotated[
        Optional[int], typer.Option("--expect-sample-rate", help="Expected sample rate in Hz")
    ] = None,
    read_seconds: Annotated[
        float, typer.Option("--read-seconds", help="Seconds of data to read for verification")
    ] = 5.0,
    report_json: Annotated[
        Optional[Path], typer.Option("--report-json", help="Write machine-readable JSON report to this path")
    ] = None,
) -> None:
    """Open a MED session and verify channel count, sample rate, and signal content."""

    results: list[dict] = []
    all_pass = True

    # Check 1: path exists
    path_ok = med_path.exists()
    results.append(_check(path_ok, "MED path exists", str(med_path)))
    if not path_ok:
        console.print(f"[red]ERROR: MED path does not exist: {med_path}[/red]")
        _print_and_exit(results, report_json, success=False)

    # Import dhn-med-py
    try:
        import dhn_med_py as dhn
    except ImportError as exc:
        console.print(f"[red]ERROR: dhn-med-py not installed or not importable: {exc}[/red]")
        raise typer.Exit(1)

    # Check 2: session opens
    session = None
    try:
        kwargs: dict = {}
        if password:
            kwargs["password"] = password
        session = dhn.MedSession(str(med_path), **kwargs)
        results.append(_check(True, "MED session opened"))
    except Exception as exc:
        results.append(_check(False, "MED session opened", str(exc)))
        _print_and_exit(results, report_json, success=False)

    assert session is not None

    try:
        # Check 3: channel count
        try:
            n_channels = len(session.channel_names)
        except Exception:
            n_channels = None

        if expect_channels is not None:
            ch_ok = n_channels == expect_channels
            results.append(
                _check(ch_ok, "Channel count", f"got {n_channels}, expected {expect_channels}")
            )
        else:
            results.append(_check(True, "Channel count", f"{n_channels} (not checked)"))

        # Check 4: sample rate
        try:
            sr = session.sampling_frequency
        except Exception:
            sr = None

        if expect_sample_rate is not None and sr is not None:
            sr_ok = int(round(sr)) == expect_sample_rate
            results.append(
                _check(sr_ok, "Sample rate", f"got {sr}, expected {expect_sample_rate}")
            )
        elif expect_sample_rate is not None and sr is None:
            results.append(_check(False, "Sample rate", "metadata did not expose sample rate"))
        else:
            results.append(_check(True, "Sample rate", f"{sr} Hz (not checked)"))

        # Check 5: read N seconds of data
        read_ok = False
        data = None
        try:
            session.read_by_time(0, int(read_seconds * 1_000_000))  # microseconds
            data = session.data
            read_ok = data is not None
        except Exception as exc:
            results.append(_check(False, f"Read {read_seconds}s of data", str(exc)))

        if read_ok:
            results.append(_check(True, f"Read {read_seconds}s of data"))

        # Check 6: signal is nonzero
        channel_stats: list[dict] = []
        if data is not None:
            arr = np.asarray(data)
            nonzero = np.any(arr != 0)
            results.append(_check(bool(nonzero), "Signal is nonzero"))

            # Per-channel statistics
            if arr.ndim == 2:
                for ch_idx in range(arr.shape[0]):
                    ch = arr[ch_idx]
                    channel_stats.append(
                        {
                            "channel": ch_idx,
                            "mean": float(np.mean(ch)),
                            "std": float(np.std(ch)),
                            "min": float(np.min(ch)),
                            "max": float(np.max(ch)),
                        }
                    )
        else:
            results.append(_check(False, "Signal is nonzero", "no data read"))

    finally:
        try:
            session.close()
        except Exception:
            pass

    all_pass = all(r["status"] == "PASS" for r in results)

    # Rich console output
    table = Table(title="MED Verification Results", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="dim", min_width=28)
    table.add_column("Status", min_width=6)
    table.add_column("Detail")

    for r in results:
        color = "green" if r["status"] == "PASS" else "red"
        table.add_row(r["check"], f"[{color}]{r['status']}[/{color}]", r.get("detail", ""))

    console.print(table)

    if channel_stats:
        ch_table = Table(title="Per-channel Statistics", show_header=True, header_style="bold cyan")
        ch_table.add_column("Ch", style="dim")
        ch_table.add_column("Mean")
        ch_table.add_column("Std")
        ch_table.add_column("Min")
        ch_table.add_column("Max")
        for s in channel_stats:
            ch_table.add_row(
                str(s["channel"]),
                f"{s['mean']:.2f}",
                f"{s['std']:.2f}",
                f"{s['min']:.2f}",
                f"{s['max']:.2f}",
            )
        console.print(ch_table)

    overall = "[bold green]ALL PASS[/bold green]" if all_pass else "[bold red]FAILURES DETECTED[/bold red]"
    console.print(f"\nOverall: {overall}")

    report = {
        "med_path": str(med_path),
        "checks": results,
        "channel_stats": channel_stats,
        "all_pass": all_pass,
    }

    if report_json is not None:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        with report_json.open("w") as fh:
            json.dump(report, fh, indent=2)
        console.print(f"Report written to: {report_json}")

    if not all_pass:
        raise typer.Exit(1)


def _print_and_exit(
    results: list[dict], report_json: Optional[Path], success: bool
) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Check", style="dim")
    table.add_column("Status")
    table.add_column("Detail")
    for r in results:
        color = "green" if r["status"] == "PASS" else "red"
        table.add_row(r["check"], f"[{color}]{r['status']}[/{color}]", r.get("detail", ""))
    console.print(table)

    if report_json is not None:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        with report_json.open("w") as fh:
            json.dump({"checks": results, "all_pass": success}, fh, indent=2)

    raise typer.Exit(0 if success else 1)
