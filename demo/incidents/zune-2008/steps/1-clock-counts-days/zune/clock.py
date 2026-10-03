from zune.rtc import days_since_origin


def boot(seconds: int) -> int:
    return days_since_origin(seconds)
