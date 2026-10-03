"""Parsing of channel files handed to us by the update service."""

LAYOUT_V3 = "v3"
LAYOUT_V4 = "v4"


def parse_record(fields: list[str], layout: str) -> dict[str, str]:
    """Turn one raw channel row into a record.

    The update service now ships two layouts, so the caller states which one it
    is handing us.
    """
    if layout == LAYOUT_V4:
        return {
            "kind": fields[0],
            "host": fields[1],
            "value": fields[5],
        }
    return {
        "kind": fields[0],
        "host": fields[1],
        "value": fields[4],
    }


def split_rows(blob: str) -> list[list[str]]:
    return [line.split(",") for line in blob.splitlines() if line]
