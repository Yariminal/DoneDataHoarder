"""
Circuit breaker pattern for AI backend resilience.

If an AI backend (e.g. Ollama) fails repeatedly, the breaker opens and
subsequent calls fail fast, allowing the system to either failover to another
backend or surface a clear error to the user instead of hanging on timeouts.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional


class CircuitBreaker:
    """
    Simple thread-safe circuit breaker.

    States:
      - CLOSED:  normal operation, failures are counted.
      - OPEN:    failure threshold reached; calls fail fast immediately.
      - HALF-OPEN: after recovery_timeout, one test call is allowed through.
                   If it succeeds → CLOSED; if it fails → OPEN again.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = timedelta(seconds=recovery_timeout_seconds)
        self._state: str = "closed"  # closed | open | half-open
        self._failures: int = 0
        self._last_failure_time: Optional[datetime] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_closed(self) -> bool:
        return self.state == "closed"

    def is_open(self) -> bool:
        return self.state == "open"

    def is_healthy(self) -> bool:
        """Return True if the breaker allows execution (closed or half-open)."""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if self._last_failure_time and (
                    datetime.utcnow() - self._last_failure_time >= self.recovery_timeout
                ):
                    self._state = "half-open"
                    return True
                return False
            # half-open
            return True

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"
            self._last_failure_time = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure_time = datetime.utcnow()
            if self._state == "half-open":
                # Test call failed → back to open
                self._state = "open"
            elif self._failures >= self.failure_threshold:
                self._state = "open"

    # ------------------------------------------------------------------
    # Decorator / wrapper helpers
    # ------------------------------------------------------------------

    def call(self, fn, *args, **kwargs):
        """Execute *fn* if breaker is healthy; otherwise raise RuntimeError."""
        if not self.is_healthy():
            raise RuntimeError(
                f"Circuit breaker is OPEN ({self._failures} consecutive failures). "
                f"Try again after {self.recovery_timeout.total_seconds():.0f}s or "
                f"switch to another AI backend."
            )
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise
