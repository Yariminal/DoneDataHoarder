"""SQLAlchemy ORM models for DataHoarder state database."""
import enum
import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
    CONTENT     = "content"      # similar document content


# ---------------------------------------------------------------------------
# File record
# ---------------------------------------------------------------------------

class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

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
    root_path: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    files_error: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
