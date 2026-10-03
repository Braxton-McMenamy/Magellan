"""Turns raw channel blobs into counted telemetry."""

from sensor.channel import LAYOUT_V4, parse_record, split_rows
from sensor.config import DEFAULT_MODE, MAX_BATCH, MODE_FAST, MODE_SAFE
from sensor.registry import note_error, remember


def _dispatch(mode: str, record: dict[str, str]) -> int:
    """Weight one record according to the collection mode."""
    match mode:
        case "fast":
            return 1
        case "safe":
            return 2
        case "legacy":
            return _legacy_weight(record)
        case _:
            note_error(f"unknown mode {mode}")
            return 0


def _legacy_weight(record: dict[str, str]) -> int:
    """Weighting kept for agents that never upgraded past the v2 channel format."""
    return 3 if record["kind"] == "probe" else 1


def process(blob: str, mode: str = DEFAULT_MODE) -> int:
    total = 0
    for row in split_rows(blob)[:MAX_BATCH]:
        record = parse_record(row, LAYOUT_V4)
        total += _dispatch(mode, record)
        remember(record["kind"], 1)
    return total


def process_fast(blob: str) -> int:
    return process(blob, MODE_FAST)

