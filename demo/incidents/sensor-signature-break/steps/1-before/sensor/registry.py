"""Process-lifetime bookkeeping for what the agent has already seen."""

_SEEN: dict[str, int] = {}
_ERRORS: list[str] = []


def remember(key: str, count: int) -> None:
    _SEEN.setdefault(key, 0)
    _SEEN.update({key: _SEEN[key] + count})


def note_error(message: str) -> None:
    _ERRORS.append(message)


def seen_count() -> int:
    return len(_SEEN)
