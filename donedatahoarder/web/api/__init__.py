"""
REST API endpoints for the DoneDataHoarder web UI.

This package used to be a single module (api.py). The endpoints now live in
per-domain routers composed into one `router` here, and every name that was
importable from `donedatahoarder.web.api` is re-exported so existing import
paths (e.g. jobs.py's `_mark_session_unsaved`) keep working.
"""
from fastapi import APIRouter

from . import (
    browse,
    dashboard,
    duplicates,
    files,
    ollama,
    pipeline,
    proposals,
    results,
    sessions,
)
from .browse import BrowseResponse, DbInfoRequest
from .deps import _mark_session_unsaved, _require_session_id, _resolve_model
from .ollama import (
    OLLAMA_HOST,
    RECOMMENDED_MODELS,
    PullModelRequest,
    StartOllamaRequest,
    _start_ollama_process,
)
from .schemas import (
    BulkApproveRequest,
    CreateSessionRequest,
    DuplicateGroupResponse,
    EditProposalRequest,
    ExecuteRequest,
    FileResponse,
    PipelineRequest,
    ProposalResponse,
    SaveSessionRequest,
    SetKeeperRequest,
    StatsResponse,
)
from .sessions import UpdateSessionSettingsRequest

router = APIRouter()
# Include order mirrors the original single-module registration order.
router.include_router(dashboard.router)
router.include_router(sessions.router)
router.include_router(files.router)
router.include_router(proposals.router)
router.include_router(duplicates.router)
router.include_router(pipeline.router)
router.include_router(browse.router)
router.include_router(ollama.router)
router.include_router(results.router)
