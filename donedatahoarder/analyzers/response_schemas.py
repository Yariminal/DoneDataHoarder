"""Contracts for the JSON emitted by file-analysis prompts.

These models are passed to the provider's retry loop. A syntactically valid
object with missing or mistyped evidence must be retried, not persisted.
"""
from __future__ import annotations

from datetime import date
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class AnalysisResponse(BaseModel):
    # AnalysisResult consumes some legacy keys (date, transcript) outside the
    # declared contract. Reject all extras so they cannot bypass validation.
    model_config = ConfigDict(extra="forbid")
    require_complete_response: ClassVar[bool] = True

    description: StrictStr
    suggested_name: StrictStr
    tags: list[StrictStr]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @field_validator("description", "suggested_name")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace text")
        return value

    @field_validator("tags")
    @classmethod
    def nonblank_tags(cls, value: list[str]) -> list[str]:
        if any(not tag.strip() for tag in value):
            raise ValueError("tags must contain non-whitespace text")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def numeric_confidence(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a JSON number")
        return value


class DatedAnalysisResponse(AnalysisResponse):
    detected_date: StrictStr | None

    @field_validator("detected_date")
    @classmethod
    def iso_date_or_null(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                raise ValueError("detected_date must be YYYY-MM-DD or null")
        return value


class ImageAnalysisResponse(DatedAnalysisResponse):
    category: Literal[
        "photo_person", "photo_group", "photo_place", "photo_event",
        "photo_document", "photo_object", "screenshot", "artwork", "other",
    ]


class DocumentAnalysisResponse(DatedAnalysisResponse):
    document_type: Literal[
        "invoice", "receipt", "contract", "report", "letter", "cv_resume",
        "photo", "presentation", "spreadsheet", "notes", "form",
        "certificate", "manual", "menu", "flyer", "brochure", "poster",
        "cover", "other",
    ]
    language: StrictStr | None

    @field_validator("language")
    @classmethod
    def iso_language_or_null(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-z]{2}", value):
            raise ValueError("language must be a lowercase ISO 639-1 code or null")
        return value


class VideoAnalysisResponse(DatedAnalysisResponse):
    video_type: Literal[
        "home_video", "event", "tutorial", "presentation",
        "screen_recording", "movie_clip", "music_video", "other",
    ]


class AudioAnalysisResponse(DatedAnalysisResponse):
    audio_type: Literal[
        "music", "podcast", "voice_memo", "lecture", "meeting_recording",
        "sound_effect", "other",
    ]


class ArchiveAnalysisResponse(DatedAnalysisResponse):
    archive_type: Literal[
        "project_backup", "software_installer", "assets_pack", "photos_album",
        "documents_bundle", "source_code", "game_files", "fonts_pack",
        "plugins_pack", "other",
    ]


class ThreeDAnalysisResponse(AnalysisResponse):
    asset_type: Literal[
        "3d_scene", "3d_character", "3d_prop", "3d_environment",
        "3d_vehicle", "3d_architecture", "3d_texture_set", "other",
    ]
    software: Literal[
        "3ds_max", "blender", "rhino", "maya", "cinema4d", "generic",
    ] | None
