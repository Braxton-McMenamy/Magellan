"""The surface the update service calls into."""

from sensor.config import MODE_FAST, MODE_SAFE
from sensor.pipeline import process
from sensor.registry import seen_count


def handle_upload(blob: str) -> dict[str, int]:
    return {"weight": process(blob, MODE_SAFE), "seen": seen_count()}


def handle_fast_upload(blob: str) -> dict[str, int]:
    return {"weight": process(blob, MODE_FAST), "seen": seen_count()}
