"""Parsing of channel files handed to us by the update service."""


def parse_record(fields: list[str]) -> dict[str, str]:
    """Turn one raw channel row into a record.

    The row layout is fixed by the update service, so positions are read
    directly.
    """
    return {
        "kind": fields[0],
        "host": fields[1],
        "value": fields[4],
    }


def split_rows(blob: str) -> list[list[str]]:
    return [line.split(",") for line in blob.splitlines() if line]
