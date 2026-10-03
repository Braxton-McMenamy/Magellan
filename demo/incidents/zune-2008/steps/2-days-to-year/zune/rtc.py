"""Real-time clock: the firmware counts days since January 1, 1980."""

ORIGIN_YEAR = 1980
SECONDS_PER_DAY = 86400


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_since_origin(seconds: int) -> int:
    return seconds // SECONDS_PER_DAY + 1
def year_from_days(days: int) -> int:
    """Calendar year of the day ``days`` (1 = January 1, 1980)."""
    year = ORIGIN_YEAR
    while days > 365:
        if is_leap_year(year):
            if days > 366:
                days -= 366
                year += 1
        else:
            days -= 365
            year += 1
    return year
