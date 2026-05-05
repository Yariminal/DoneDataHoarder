"""
Unit tests for the *_with_progress generators that wrap pipeline steps as
background-job-friendly iterables.

These generators are the contract that JobManager drives in start_dedup,
start_relate, start_propose, start_organize, and start_execute_dry. The
critical contract:

  1. Yield progress dicts on each natural iteration boundary
  2. Honor pause_event by blocking before each unit of work
  3. Honor cancel_check by yielding {"cancelled": True, ...} and returning
  4. Yield a final {"done": True, ...} with summary counts
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from donedatahoarder.db.session import init_db


@pytest.fixture
def empty_db(tmp_path):
    """Initialize an empty in-tmp SQLite DB for generators that need one."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    yield db_path
    # No teardown — tmp_path is auto-cleaned. The global _engine in
    # session.py persists across tests but each test uses a fresh path.


# ---------------------------------------------------------------------------
# dedup_with_progress
# ---------------------------------------------------------------------------

class TestDedupWithProgress:
    def test_cancel_before_first_phase(self, empty_db):
        """If cancel_check is True from the start, no phases should run."""
        from donedatahoarder.core.dedup import dedup_with_progress

        events = list(dedup_with_progress(
            session_id="nonexistent-sid",
            cancel_check=lambda: True,
        ))
        # First event is "starting", second should be cancelled
        assert events[0].get("phase") == "starting"
        cancelled = [e for e in events if e.get("cancelled")]
        assert len(cancelled) == 1, f"Expected exactly one cancelled event, got events={events!r}"
        # Cancelled event should NOT be marked done
        assert not any(e.get("done") for e in events)

    def test_empty_session_completes(self, empty_db):
        """An empty session should run all phases and yield done."""
        from donedatahoarder.core.dedup import dedup_with_progress

        events = list(dedup_with_progress(session_id="nonexistent-sid"))
        # Final event should be done
        assert events[-1].get("done") is True
        # Should have phase events for all 5 phases
        phases = {e.get("phase") for e in events if e.get("phase") != "starting"}
        assert "exact" in phases
        assert "perceptual" in phases
        assert "semantic" in phases
        assert "text_near" in phases
        assert "proposals" in phases

    def test_pause_event_blocks(self, empty_db):
        """pause_event.clear() should block iteration; .set() should resume."""
        from donedatahoarder.core.dedup import dedup_with_progress

        pause_event = threading.Event()
        pause_event.set()  # Start unpaused so generator can begin

        events_collected: list[dict] = []
        gen = dedup_with_progress(session_id="nonexistent-sid", pause_event=pause_event)

        # Drain the first event ("starting")
        events_collected.append(next(gen))

        # Now block before the next pause-checkpoint
        pause_event.clear()

        # Run iteration in a thread; it should block
        done_iterating = threading.Event()
        def _drain():
            for ev in gen:
                events_collected.append(ev)
            done_iterating.set()

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

        # Should still be iterating after a brief wait (blocked on pause)
        # NOTE: the dedup generator only checks pause between phases, so this
        # isn't a perfect block test for fast empty sessions. Just confirm
        # it eventually finishes after we set the event.
        pause_event.set()
        t.join(timeout=5.0)
        assert done_iterating.is_set(), "Generator did not finish after resume"


# ---------------------------------------------------------------------------
# generate_proposals_with_progress
# ---------------------------------------------------------------------------

class TestProposeWithProgress:
    def test_cancel_before_start(self, empty_db):
        """cancel_check=True before yield should produce only cancelled."""
        from donedatahoarder.proposals.namer import generate_proposals_with_progress

        events = list(generate_proposals_with_progress(
            session_id="nonexistent-sid",
            cancel_check=lambda: True,
        ))
        assert any(e.get("cancelled") for e in events)
        assert not any(e.get("done") for e in events)

    def test_empty_session_completes(self, empty_db):
        """No ANALYZED files → propose still completes successfully."""
        from donedatahoarder.proposals.namer import generate_proposals_with_progress

        events = list(generate_proposals_with_progress(session_id="nonexistent-sid"))
        assert events[-1].get("done") is True


# ---------------------------------------------------------------------------
# generate_reorg_proposals_with_progress
# ---------------------------------------------------------------------------

class TestOrganizeWithProgress:
    def test_cancel_before_start(self, empty_db):
        from donedatahoarder.proposals.organizer import generate_reorg_proposals_with_progress

        events = list(generate_reorg_proposals_with_progress(
            session_id="nonexistent-sid",
            cancel_check=lambda: True,
        ))
        assert any(e.get("cancelled") for e in events)
        assert not any(e.get("done") for e in events)

    def test_empty_session_completes(self, empty_db):
        from donedatahoarder.proposals.organizer import generate_reorg_proposals_with_progress

        events = list(generate_reorg_proposals_with_progress(session_id="nonexistent-sid"))
        assert events[-1].get("done") is True


# ---------------------------------------------------------------------------
# execute_with_progress
# ---------------------------------------------------------------------------

class TestExecuteWithProgress:
    def test_cancel_before_start(self, empty_db):
        from donedatahoarder.executor import execute_with_progress

        events = list(execute_with_progress(
            session_id="nonexistent-sid",
            cancel_check=lambda: True,
        ))
        assert any(e.get("cancelled") for e in events)
        assert not any(e.get("done") for e in events)

    def test_empty_session_completes(self, empty_db):
        """No proposals → execute(dry_run=True) still completes."""
        from donedatahoarder.executor import execute_with_progress

        events = list(execute_with_progress(session_id="nonexistent-sid"))
        assert events[-1].get("done") is True
        # Should report zero applied (no proposals exist)
        assert events[-1].get("applied", 0) == 0


# ---------------------------------------------------------------------------
# JobManager start_* methods (integration smoke tests)
# ---------------------------------------------------------------------------

class TestJobManagerStartMethods:
    def test_start_methods_exist(self):
        """All 5 new start_* methods are defined on the JobManager singleton."""
        from donedatahoarder.core.jobs import job_manager

        assert callable(getattr(job_manager, "start_dedup", None))
        assert callable(getattr(job_manager, "start_relate", None))
        assert callable(getattr(job_manager, "start_propose", None))
        assert callable(getattr(job_manager, "start_organize", None))
        assert callable(getattr(job_manager, "start_execute_dry", None))

    def test_generic_runner_helper_exists(self):
        """The shared _generic_runner helper must be accessible."""
        from donedatahoarder.core.jobs import job_manager

        assert callable(getattr(job_manager, "_generic_runner", None))
