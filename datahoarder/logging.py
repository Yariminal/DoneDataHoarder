"""
Structured logging setup for DataHoarder.

Usage:
    from datahoarder.logging import get_logger
    logger = get_logger(__name__)
    logger.info("Scan complete", extra={"files_new": 42})

Environment:
    DATAHOARDER_LOG=DEBUG   -> debug level to console + file
    --verbose               -> same, set by CLI flag

Log files are written to ~/.datahoarder/datahoarder.log at INFO level.
Console output respects Rich theming (typer/rich) and only shows
WARNING+ unless --verbose is passed.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler

_DEFAULT_LOG_DIR = Path.home() / ".datahoarder"
_DEFAULT_LOG_FILE = _DEFAULT_LOG_DIR / "datahoarder.log"

_LOG_LEVEL_NAMES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class ExtraLogAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges 'extra' dicts into every message seamlessly."""

    def process(self, msg, kwargs):
        extra = kwargs.pop("extra", {})
        merged = {**self.extra, **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def setup_logging(
    verbose: bool = False,
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """
    Configure root DataHoarder logger.

    Args:
        verbose: If True, set console level to DEBUG; otherwise WARNING.
        log_file: Path to the persistent log file. Defaults to ~/.datahoarder/datahoarder.log.
    """
    env_level = os.environ.get("DATAHOARDER_LOG", "").lower()
    level = _LOG_LEVEL_NAMES.get(env_level, logging.DEBUG if verbose else logging.INFO)
    console_level = logging.DEBUG if verbose or env_level == "debug" else logging.WARNING

    root_logger = logging.getLogger("datahoarder")
    root_logger.setLevel(level)

    # Prevent double-registration
    if root_logger.handlers:
        root_logger.handlers.clear()

    # ---- Console handler (Rich, pretty, no timestamps) ----
    console_handler = RichHandler(
        rich_tracebacks=True,
        show_time=False,
        show_path=verbose or env_level == "debug",
    )
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter(
        fmt="%(message)s",
        datefmt="[%X]",
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # ---- File handler (structured, timestamps, always INFO+) ----
    file_path = log_file or _DEFAULT_LOG_FILE
    _ensure_dir(file_path.parent)
    file_handler = logging.FileHandler(str(file_path), encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Also capture critical SQLAlchemy / urllib3 noise at WARNING+
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    root_logger.debug(
        "Logging initialized",
        extra={
            "console_level": logging.getLevelName(console_level),
            "file_level": "INFO",
            "log_file": str(file_path),
        },
    )
    return root_logger


def get_logger(name: str) -> ExtraLogAdapter:
    """Return a logger adapter with optional structured extras."""
    logger = logging.getLogger(name)
    return ExtraLogAdapter(logger, {})
