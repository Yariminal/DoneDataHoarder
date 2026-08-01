"""Time helpers shared across the codebase."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """
    Naive UTC now — replacement for the deprecated ``datetime.utcnow()``.

    The DB schema and API responses use naive UTC datetimes throughout
    (SQLite DateTime columns, ``.isoformat()`` without offset). Returning an
    aware datetime here would change serialized formats and raise on
    comparisons against stored naive values, so the tzinfo is dropped
    deliberately.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
