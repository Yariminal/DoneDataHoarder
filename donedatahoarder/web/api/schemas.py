"""
Pydantic request/response models shared across the API routers.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class StatsResponse(BaseModel):
    total_files: int = 0
    total_size_bytes: int = 0
    by_status: dict[str, int] = {}
    by_extension: list[dict] = []
    by_mime_category: list[dict] = []
    proposal_counts: dict[str, int] = {}
    duplicate_groups: int = 0
    duplicate_wasted_bytes: int = 0


class FileResponse(BaseModel):
    id: int
    path: str
    filename: str
    extension: Optional[str] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    status: str
    date_best: Optional[str] = None
    ai_description: Optional[str] = None
    ai_tags: Optional[list[str]] = None
    ai_confidence: Optional[float] = None
    ai_model: Optional[str] = None
    proposals: list[dict] = []


class ProposalResponse(BaseModel):
    id: int
    file_id: int
    filename: str
    proposal_type: str
    current_value: Optional[str] = None
    proposed_value: Optional[str] = None
    reasoning: Optional[str] = None
    confidence: Optional[float] = None
    status: str
    mime_type: Optional[str] = None


class DuplicateGroupResponse(BaseModel):
    id: int
    dupe_type: str
    count: int
    keep_file_id: Optional[int] = None
    wasted_bytes: int = 0
    files: list[dict] = []


class BulkApproveRequest(BaseModel):
    session_id: str = Field(min_length=1)
    min_confidence: float = Field(default=0.8, ge=0, le=1)
    proposal_type: Optional[str] = None


class BulkRejectRequest(BaseModel):
    session_id: str = Field(min_length=1)


class ReviewProposalRequest(BaseModel):
    session_id: str = Field(min_length=1)


class EditProposalRequest(BaseModel):
    session_id: str = Field(min_length=1)
    proposed_value: str


class SetKeeperRequest(BaseModel):
    session_id: str = Field(min_length=1)
    keep_file_id: int


class PipelineRequest(BaseModel):
    root_path: str = ""
    backend: str = "ollama"
    model: str = ""
    workers: int = 1
    session_id: str = ""
    skip_dirs: list[str] = []
    retry_errors: bool = False
    sequence_sample_stride: int = Field(default=0, ge=0, le=1000)
    use_cache: bool = True


class RunPlanRequest(PipelineRequest):
    """Persisted unattended pipeline; commit is deliberately excluded."""
    steps: list[str] = [
        "scan", "enrich", "analyze", "dedup", "relate", "propose",
        "organize", "execute_dry",
    ]
    analyze_model: str = ""
    propose_model: str = ""
    relate_scope: str = "per_directory"


class ResumeRunPlanRequest(BaseModel):
    session_id: str = Field(min_length=1)
    retry_errors: bool = False


class CreateSessionRequest(BaseModel):
    root_path: str = ""
    backend: str = "ollama"
    model: str = "gemma3:12b"
    analyze_model: str = ""
    propose_model: str = ""
    workers: int = 1
    preferred_language: str = "leave_as_is"
    # "per_directory" (default, cheap) or "cross_directory" (whole-tree relate).
    relate_scope: str = "per_directory"


class SaveSessionRequest(BaseModel):
    name: str


class ExecuteRequest(BaseModel):
    session_id: str = ""
    dry_run: bool = True
    min_confidence: float = 0.7
    preview_token: Optional[str] = None
