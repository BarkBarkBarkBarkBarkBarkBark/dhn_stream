"""Channel label loading for the optional dashboard."""

from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ChannelLabel:
    channel: int
    name: str
    description: str = ""
    electrode_type: str = "unknown"
    source: str = "fallback"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def load_channel_config_csv(path: str | Path) -> dict[int, ChannelLabel]:
    """Load DHN_Acq channel names/descriptions from a CSV or TSV channel settings file."""
    p = Path(path)
    delimiter = "\t" if p.suffix.lower() == ".tsv" else ","
    labels: dict[int, ChannelLabel] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            return labels
        field_map = {_normalise_key(name): name for name in reader.fieldnames}
        number_key = field_map.get("channel_number")
        name_key = field_map.get("channel_name")
        description_key = field_map.get("channel_description")
        if number_key is None:
            raise ValueError(f"No Channel Number column found in {p}")
        for row in reader:
            raw_number = (row.get(number_key) or "").strip()
            if not raw_number:
                continue
            try:
                channel = int(raw_number)
            except ValueError:
                continue
            name = (row.get(name_key) or f"ch{channel:04d}").strip() if name_key else f"ch{channel:04d}"
            description = (row.get(description_key) or "").strip() if description_key else ""
            labels[channel] = ChannelLabel(
                channel=channel,
                name=name or f"ch{channel:04d}",
                description=description,
                source=str(p),
            )
    return labels


def load_connection_map(path: str | Path) -> dict[int, ChannelLabel]:
    """Load channel labels from CSV/TSV/XLSX connection maps using best-effort columns."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return _load_connection_map_rows(p, _read_csv_rows(p))
    if suffix == ".xlsx":
        return _load_connection_map_xlsx(p)
    raise ValueError(f"Unsupported connection map format: {p.suffix}")


def merge_labels(
    n_channels: int,
    *sources: dict[int, ChannelLabel],
) -> list[ChannelLabel]:
    """Merge label sources by precedence: later sources override earlier sources."""
    merged: dict[int, ChannelLabel] = {}
    for channel in range(1, n_channels + 1):
        merged[channel] = ChannelLabel(channel=channel, name=f"ch{channel:04d}")
    for source in sources:
        for channel, label in source.items():
            if 1 <= channel <= n_channels:
                merged[channel] = label
    return [merged[channel] for channel in range(1, n_channels + 1)]


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def _load_connection_map_xlsx(path: Path) -> dict[int, ChannelLabel]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ImportError("XLSX connection maps require the optional 'openpyxl' package") from exc

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        all_rows: list[dict[str, Any]] = []
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            header_index = _find_header_index(rows)
            if header_index is None:
                continue
            headers = [str(value).strip() if value is not None else "" for value in rows[header_index]]
            for row in rows[header_index + 1 :]:
                record = {headers[idx]: value for idx, value in enumerate(row) if idx < len(headers)}
                record["_sheet"] = sheet.title
                all_rows.append(record)
        return _load_connection_map_rows(path, all_rows)
    finally:
        workbook.close()


def _find_header_index(rows: list[tuple[Any, ...]]) -> int | None:
    for idx, row in enumerate(rows[:20]):
        keys = {_normalise_key(str(value)) for value in row if value is not None}
        if any("channel" in key or key in {"ch", "chan"} for key in keys):
            return idx
    return None


def _load_connection_map_rows(path: Path, rows: list[dict[str, Any]]) -> dict[int, ChannelLabel]:
    labels: dict[int, ChannelLabel] = {}
    for row in rows:
        normalised = {_normalise_key(str(key)): value for key, value in row.items() if key is not None}
        channel = _first_int(normalised, ("channel", "channel_number", "ch", "chan", "dhn_channel"))
        if channel is None:
            continue
        name = _first_text(normalised, ("label", "name", "channel_name", "electrode", "contact"))
        description = _first_text(normalised, ("description", "notes", "region", "location", "target"))
        electrode_type = _infer_electrode_type(" ".join(str(value) for value in normalised.values()))
        labels[channel] = ChannelLabel(
            channel=channel,
            name=name or f"ch{channel:04d}",
            description=description,
            electrode_type=electrode_type,
            source=str(path),
        )
    return labels


def _first_int(row: dict[str, Any], candidates: tuple[str, ...]) -> int | None:
    for key in candidates:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return int(float(str(value).strip()))
        except ValueError:
            continue
    return None


def _first_text(row: dict[str, Any], candidates: tuple[str, ...]) -> str:
    for key in candidates:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _infer_electrode_type(text: str) -> str:
    lowered = text.lower()
    if "micro" in lowered:
        return "micro"
    if "macro" in lowered:
        return "macro"
    return "unknown"
