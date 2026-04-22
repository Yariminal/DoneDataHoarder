"""
Unit tests for datahoarder.ai.circuit_breaker.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from datahoarder.ai.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.is_closed()
        assert cb.is_healthy()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_healthy()  # not yet open
        cb.record_failure()
        assert not cb.is_healthy()  # open now

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.is_closed()
        cb.record_failure()
        assert cb.is_healthy()  # only 1 failure after reset

    def test_half_open_then_success(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.1)
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.15)
        assert cb.is_healthy()  # half-open
        cb.record_success()
        assert cb.is_closed()

    def test_half_open_then_failure(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.is_healthy()  # half-open
        cb.record_failure()
        assert cb.is_open()

    def test_call_success(self):
        cb = CircuitBreaker()
        result = cb.call(lambda: 42)
        assert result == 42
        assert cb.is_closed()

    def test_call_failure(self):
        cb = CircuitBreaker(failure_threshold=1)

        def boom():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            cb.call(boom)
        assert cb.is_open()

    def test_call_open_raises_runtime(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        with pytest.raises(RuntimeError) as exc_info:
            cb.call(lambda: 42)
        assert "OPEN" in str(exc_info.value)

    def test_thread_safety(self):
        cb = CircuitBreaker(failure_threshold=100)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    cb.record_failure()
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for _ in range(8):
                pool.submit(worker)

        assert not errors
        assert cb._failures == 160
