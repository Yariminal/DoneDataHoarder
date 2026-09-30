"""Parse common media tag dates without truncating their precision."""
import re
from datetime import datetime


def parse_media_date(raw: str) -> datetime | None:
    """Return a naive capture/release date, or None for malformed tags.

    Media dates express wall-clock time; offsets are deliberately discarded
    to match the existing naive metadata dates stored in the index.
    """
    raw = raw.strip()
    try:
        if re.fullmatch(r"\d{4}", raw):
            return datetime(int(raw), 1, 1)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.strptime(raw, "%Y-%m-%d")
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
            raw,
        ):
            # Python 3.10 requires exactly 3 or 6 fractional digits, while
            # newer versions accept other lengths. Keep tag parsing stable
            # across the supported runtime versions at microsecond precision.
            normalized = re.sub(r"\.(\d+)",
                                lambda match: "." + match[1][:6].ljust(6, "0"), raw)
            return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    return None
