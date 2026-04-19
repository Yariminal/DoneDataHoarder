"""SQLAlchemy ORM models for DataHoarder state database."""
import enum
import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Session — top-level container for an entire DataHoarder run
# ---------------------------------------------------------------------------

class SessionStatus(str, enum.Enum):
    NEW       = "new"        # just created, no data yet
    ACTIVE    = "active"     # scan has run, data exists
    COMPLETED = "completed"  # user explicitly marked done


class UserSession(Base):
    """A session groups all files, proposals, and duplicates from one run."""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_saved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Configuration captured at session creation
    root_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    backend: Mapped[str] = mapped_column(String, default="ollama")
    model: Mapped[str] = mapped_column(String, default="llama3.2:3b")
    analyze_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    propose_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    workers: Mapped[int] = mapped_column(Integer, default=1)
    preferred_language: Mapped[str] = mapped_column(String, default="leave_as_is")
    # Relate step scope — "per_directory" (default, cheap) or "cross_directory"
    # (whole-tree, catches cross-folder clusters but larger prompts).
    relate_scope: Mapped[str] = mapped_column(String, default="per_directory")

    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.NEW
    )
    is_unsaved: Mapped[bool] = mapped_column(Boolean, default=False)

    # JSON blob: {files_count, proposals_count, duplicates_count, completed_steps: []}
    stats_json: Mapped[Optional[str]] = mapped_column(Text, default="{}")

    # --- relationships ---
    files: Mapped[list["File"]] = relationship(
        "File", back_populates="session", cascade="all, delete-orphan"
    )

    @property
    def stats(self) -> dict:
        try:
            return json.loads(self.stats_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    @stats.setter
    def stats(self, value: dict):
        self.stats_json = json.dumps(value)

    def __repr__(self) -> str:
        return f"<UserSession id={self.id!r} name={self.name!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FileStatus(str, enum.Enum):
    PENDING   = "pending"    # scanned, nothing else done
    ENRICHED  = "enriched"   # metadata + hashes extracted
    ANALYZED  = "analyzed"   # AI analysis complete
    PROPOSED  = "proposed"   # rename/org proposals generated
    SKIPPED   = "skipped"    # user chose to skip this file
    APPLIED   = "applied"    # proposals executed
    ERROR     = "error"      # processing failed


class ProposalType(str, enum.Enum):
    RENAME           = "rename"
    RENAME_FOLDER    = "rename_folder"
    MOVE             = "move"
    ADD_TAGS         = "add_tags"
    UPDATE_METADATA  = "update_metadata"
    MARK_DUPLICATE   = "mark_duplicate"


class ProposalStatus(str, enum.Enum):
    PENDING  = "pending"   # awaiting human review
    APPROVED = "approved"  # human approved
    REJECTED = "rejected"  # human rejected
    MODIFIED = "modified"  # human edited the proposal value
    APPLIED  = "applied"   # change has been executed on disk


class DupeType(str, enum.Enum):
    EXACT       = "exact"        # identical MD5
    PERCEPTUAL  = "perceptual"   # near-identical image (pHash)
    SEMANTIC    = "semantic"     # similar AI description/tags (same content type)
    CONTENT     = "content"      # similar document content


# ---------------------------------------------------------------------------
# File record
# ---------------------------------------------------------------------------

class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- session ---
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    session: Mapped["UserSession"] = relationship("UserSession", back_populates="files")

    # --- identity ---
    path: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    extension: Mapped[Optional[str]] = mapped_column(String)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    mime_type: Mapped[Optional[str]] = mapped_column(String)

    # --- hashes ---
    hash_md5: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    hash_sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    hash_perceptual: Mapped[Optional[str]] = mapped_column(String, index=True)  # images

    # --- dates ---
    date_modified: Mapped[Optional[datetime]] = mapped_column(DateTime)
    date_created: Mapped[Optional[datetime]] = mapped_column(DateTime)
    date_exif: Mapped[Optional[datetime]] = mapped_column(DateTime)   # most reliable for photos
    date_best: Mapped[Optional[datetime]] = mapped_column(DateTime)   # best guess (exif > modified > created)

    # --- AI output ---
    ai_description: Mapped[Optional[str]] = mapped_column(Text)
    ai_suggested_name: Mapped[Optional[str]] = mapped_column(String)  # AI-proposed filename stem
    ai_tags: Mapped[Optional[str]] = mapped_column(Text)        # JSON array string
    ai_transcript: Mapped[Optional[str]] = mapped_column(Text)  # video/audio transcript
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_model: Mapped[Optional[str]] = mapped_column(String)     # which model was used

    # --- status ---
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus), default=FileStatus.PENDING, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # --- timestamps ---
    scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # --- relationships ---
    proposals: Mapped[list["Proposal"]] = relationship(
        "Proposal", back_populates="file", cascade="all, delete-orphan"
    )

    def tags_list(self) -> list[str]:
        if not self.ai_tags:
            return []
        try:
            return json.loads(self.ai_tags)
        except (json.JSONDecodeError, TypeError):
            return []

    def __repr__(self) -> str:
        return f"<File id={self.id} status={self.status} path={self.path!r}>"


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id"), nullable=False)

    proposal_type: Mapped[ProposalType] = mapped_column(Enum(ProposalType), nullable=False)
    current_value: Mapped[Optional[str]] = mapped_column(Text)
    proposed_value: Mapped[Optional[str]] = mapped_column(Text)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus), default=ProposalStatus.PENDING, index=True
    )
    user_notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    file: Mapped["File"] = relationship("File", back_populates="proposals")

    def __repr__(self) -> str:
        return (
            f"<Proposal id={self.id} type={self.proposal_type} "
            f"status={self.status} file_id={self.file_id}>"
        )


# ---------------------------------------------------------------------------
# Duplicate groups
# ---------------------------------------------------------------------------

class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    dupe_type: Mapped[DupeType] = mapped_column(Enum(DupeType), nullable=False)
    group_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # The file we decided to keep (null = undecided)
    keep_file_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("files.id"))

    members: Mapped[list["DuplicateMember"]] = relationship(
        "DuplicateMember", back_populates="group", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("dupe_type", "group_hash", name="uq_dupe_group"),
    )


class DuplicateMember(Base):
    __tablename__ = "duplicate_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("duplicate_groups.id"), nullable=False)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id"), nullable=False)
    similarity_score: Mapped[Optional[float]] = mapped_column(Float)  # 1.0 = identical

    group: Mapped["DuplicateGroup"] = relationship("DuplicateGroup", back_populates="members")

    __table_args__ = (
        UniqueConstraint("group_id", "file_id", name="uq_dupe_member"),
    )


# ---------------------------------------------------------------------------
# Scan sessions (for tracking / resuming)
# ---------------------------------------------------------------------------

class ScanSession(Base):
    __tablename__ = "scan_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    root_path: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    files_error: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Relation groups — semantic clusters of related files (shared identity,
# not identical content). E.g. a .dwg AutoCAD source with its .bak backup
# and .pdf exports; a .psd with its exported .jpg; a multi-version plan.
# Produced by the `relate` pipeline step using an LLM on filenames (fast),
# with a regex prefix-cluster backstop for reliability.
# ---------------------------------------------------------------------------

class RelationRole(str, enum.Enum):
    SOURCE  = "source"    # master / canonical file (e.g. .dwg, .psd)
    EXPORT  = "export"    # derivative output (e.g. PDF exported from CAD)
    BACKUP  = "backup"    # automatic backup (e.g. .bak, .3dmbak)
    VERSION = "version"   # revised variant of another member
    SIBLING = "sibling"   # related but peer (no source/export hierarchy)


class RelationGroup(Base):
    __tablename__ = "relation_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Short snake_case slug usable as a folder name (≤40 chars).
    label: Mapped[str] = mapped_column(String, nullable=False)
    # One-sentence explanation of what binds these files.
    reason: Mapped[Optional[str]] = mapped_column(Text)
    # 0.3 = algorithmic backstop, 0.8 = LLM-reasoned group.
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # "per_directory" or "cross_directory" — how the group was discovered.
    scope: Mapped[str] = mapped_column(String, default="per_directory")
    # Parent directory for per_directory groups (null for cross_directory).
    dir_path: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list["RelationMember"]] = relationship(
        "RelationMember", back_populates="group", cascade="all, delete-orphan"
    )


class RelationMember(Base):
    __tablename__ = "relation_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("relation_groups.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    role: Mapped[RelationRole] = mapped_column(
        Enum(RelationRole), default=RelationRole.SIBLING
    )

    group: Mapped["RelationGroup"] = relationship("RelationGroup", back_populates="members")

    __table_args__ = (
        UniqueConstraint("group_id", "file_id", name="uq_relation_member"),
    )


# ---------------------------------------------------------------------------
# CompletedFolder — tracks folders that finished the full pipeline
# ---------------------------------------------------------------------------

class CompletedFolder(Base):
    __tablename__ = "completed_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    root_path: Mapped[str] = mapped_column(String, nullable=False)
