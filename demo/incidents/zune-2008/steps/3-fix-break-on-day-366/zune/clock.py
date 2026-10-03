from zune.rtc import days_since_origin, year_from_days


def boot(seconds: int) -> int:
    return year_from_days(days_since_origin(seconds))
