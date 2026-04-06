"""Utilities for handling Rich progress in web context."""
import sys
from contextlib import contextmanager
from rich.progress import Progress


@contextmanager
def get_progress(*columns, **kwargs):
    """
    Context manager that returns a Progress bar if running in terminal,
    or a dummy no-op context if running in web/non-TTY context.
    """
    # Check if we're in a TTY (terminal) context
    # In web/API context, stdout is not a TTY
    if hasattr(sys.stdout, 'isatty') and not sys.stdout.isatty():
        # Running in web context - use dummy context manager
        class DummyProgress:
            def add_task(self, description, total=None):
                return 0
            def update(self, task_id, **kwargs):
                pass
        yield DummyProgress()
    else:
        # Running in terminal - use real Progress
        try:
            with Progress(*columns, **kwargs) as progress:
                yield progress
        except (UnicodeEncodeError, OSError):
            # Fallback if Unicode fails (Windows codepage issues)
            class DummyProgress:
                def add_task(self, description, total=None):
                    return 0
                def update(self, task_id, **kwargs):
                    pass
            yield DummyProgress()
