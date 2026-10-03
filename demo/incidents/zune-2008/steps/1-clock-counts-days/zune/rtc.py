"""Real-time clock: the firmware counts days since January 1, 1980."""

ORIGIN_YEAR = 1980
SECONDS_PER_DAY = 86400


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_since_origin(seconds: int) -> int:
    return seconds // SECONDS_PER_DAY + 1
