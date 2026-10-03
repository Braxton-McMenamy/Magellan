"""The surface the update service calls into."""

from sensor.config import MODE_FAST, MODE_LEGACY, MODE_SAFE
from sensor.pipeline import _dispatch, process
from sensor.registry import seen_count


def handle_upload(blob: str) -> dict[str, int]:
    return {"weight": process(blob, MODE_SAFE), "seen": seen_count()}


def handle_fast_upload(blob: str) -> dict[str, int]:
    return {"weight": process(blob, MODE_FAST), "seen": seen_count()}


def handle_legacy_upload(blob: str) -> dict[str, int]:
    """Older agents still post to this endpoint."""
    return {"weight": process(blob, MODE_LEGACY), "seen": seen_count()}


def weigh_one(record: dict[str, str]) -> int:
    return _dispatch(MODE_LEGACY, record)
