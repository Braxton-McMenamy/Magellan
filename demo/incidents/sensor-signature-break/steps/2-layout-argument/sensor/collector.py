"""The scheduled sweep path. Shares the channel parser with the upload path."""

from sensor.channel import parse_record, split_rows
from sensor.registry import note_error


def collect(blob: str) -> list[dict[str, str]]:
    records = []
    for row in split_rows(blob):
        try:
            records.append(parse_record(row))
        except Exception:
            pass
    return records


def sweep(blob: str) -> int:
    records = collect(blob)
    if not records:
        note_error("empty sweep")
    return len(records)
