"""Agent entrypoint."""

from sensor.api import handle_upload
from sensor.collector import sweep
from sensor.registry import seen_count


def run_agent(blob: str) -> int:
    result = handle_upload(blob)
    sweep(blob)
    return result["weight"]


if __name__ == "__main__":
    import sys

    print(run_agent(sys.stdin.read()), seen_count())
