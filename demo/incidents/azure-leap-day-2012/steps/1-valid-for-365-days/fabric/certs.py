"""Transfer certificates the guest agent creates to receive secrets from the host."""

from datetime import datetime, timedelta, timezone


def transfer_certificate_validity(now: datetime | None = None) -> tuple[datetime, datetime]:
    """When a new transfer certificate starts and stops being valid."""
    now = now or datetime.now(timezone.utc)
    return now, now + timedelta(days=365)
