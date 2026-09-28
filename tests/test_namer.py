"""
Unit tests for donedatahoarder.proposals.namer — naming heuristics.
"""
from pathlib import Path
from datetime import datetime

import pytest

from donedatahoarder.proposals.namer import (
    _is_useless_stem,
    _hygienic_stem,
    _safe,
)
from donedatahoarder.proposals.namer.core import (
    _content_verified_for_naming, _preserves_descriptive_identity,
    _restore_descriptive_identity, _suppress_unsafe_rename_postpasses,
)
from types import SimpleNamespace
from donedatahoarder.proposals.namer.naming import (
    _ensure_prefix, _is_meaningful_date, _name_date_provenance, _source_filename_date,
    build_new_name,
)


def _dated_name_record(path: Path, *, suggested: str, modified=None, created=None,
                       exif=None):
    return SimpleNamespace(
        path=str(path), ai_suggested_name=suggested,
        ai_description="Brief subject description", ai_tags=None,
        ai_confidence=0.9, date_modified=modified, date_created=created,
        date_best=exif or modified or created, date_exif=exif,
    )


def test_copied_document_mtime_cannot_create_a_new_date_prefix(tmp_path):
    row = _dated_name_record(
        tmp_path / "report.docx", suggested="policy_summary",
        modified=datetime(2018, 1, 17), created=datetime(2026, 9, 28),
    )
    assert not _is_meaningful_date(row)
    assert build_new_name(row, root_path=str(tmp_path)) == "policy_summary.docx"
    assert _name_date_provenance(row, "policy_summary") == ""
    row.ai_suggested_name = "2018-01-17_policy_summary"
    assert build_new_name(row, root_path=str(tmp_path)) == "policy_summary.docx"


def test_existing_full_date_is_retained_as_filename_identity(tmp_path):
    row = _dated_name_record(
        tmp_path / "notes_2021.10.12.docx", suggested="meeting_notes",
        modified=datetime(2019, 5, 1), created=datetime(2026, 9, 28),
    )
    assert _source_filename_date(Path(row.path).stem) == "2021-10-12"
    proposed = build_new_name(row, root_path=str(tmp_path))
    assert proposed == "2021-10-12_meeting_notes.docx"
    assert "original filename" in _name_date_provenance(row, Path(proposed).stem)
    assert _source_filename_date("notes_2021.02.30") is None
    row.path = str(tmp_path / "2021.10.12_report.docx")
    proposed = build_new_name(row, root_path=str(tmp_path))
    assert proposed == "2021-10-12_meeting_notes.docx"
    assert _ensure_prefix(Path(proposed).stem, Path(row.path).stem) == Path(proposed).stem
    row.path = str(tmp_path / "10.8_notes_2021-10-12.docx")
    proposed = build_new_name(row, root_path=str(tmp_path))
    assert proposed == "2021-10-12_meeting_notes.docx"
    assert _ensure_prefix(Path(proposed).stem, Path(row.path).stem).startswith("10_8_2021-10-12_")


def test_project_year_identifier_is_not_replaced_by_file_mtime(tmp_path):
    row = _dated_name_record(
        tmp_path / "PROJECTQ_B_2022.pptx", suggested="annual_report",
        modified=datetime(2021, 10, 12), created=datetime(2026, 9, 28),
    )
    proposed = build_new_name(row, root_path=str(tmp_path))
    assert proposed == "annual_report.pptx"
    assert _restore_descriptive_identity(Path(row.path).stem, Path(proposed).stem).startswith("PROJECTQ_B_2022_")


def test_stored_photo_exif_date_is_used_without_claiming_capture_time(tmp_path):
    row = _dated_name_record(
        tmp_path / "IMG_1234.jpg", suggested="family_portrait",
        modified=datetime(2026, 9, 28), created=datetime(2026, 9, 28),
        exif=datetime(2020, 5, 6, 12, 34, 56),
    )
    assert _is_meaningful_date(row)
    proposed = build_new_name(row, root_path=str(tmp_path))
    assert proposed == "2020-05-06_12-34-56_family_portrait.jpg"
    note = _name_date_provenance(row, Path(proposed).stem)
    assert "stored EXIF metadata" in note and "capture date unverified" in note
    row.path = str(tmp_path / "clip.mp4")
    assert not _is_meaningful_date(row)
    assert build_new_name(row, root_path=str(tmp_path)) == "family_portrait.mp4"
    row.path = str(tmp_path / "IMG_1234.jpg")
    row.date_exif = "2020-05-06"
    assert not _is_meaningful_date(row)
    assert build_new_name(row, root_path=str(tmp_path)) == "family_portrait.jpg"
    row.date_exif = datetime(1900, 5, 6)
    assert not _is_meaningful_date(row)


def test_rename_proposal_explains_only_supported_date_prefix(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from donedatahoarder.db.models import File, FileStatus, Proposal, ProposalType, UserSession
    from donedatahoarder.db.session import init_db
    from donedatahoarder.proposals.namer.core import generate_proposals

    monkeypatch.setenv("DDH_DATA_DIR", str(tmp_path / "state"))
    root = tmp_path / "collection"
    root.mkdir()
    engine = init_db(tmp_path / "naming-dates.sqlite")
    with Session(engine) as db:
        owner = UserSession(root_path=str(root))
        db.add(owner)
        db.flush()
        for name, source, suggestion, modified, exif in [
            ("report.docx", "text", "policy_brief", datetime(2018, 1, 17), None),
            ("IMG_1234.jpg", "vision", "family_portrait", datetime(2026, 9, 28),
             datetime(2020, 5, 6, 12, 34, 56)),
        ]:
            path = root / name
            path.write_bytes(b"synthetic")
            db.add(File(
                session_id=owner.id, path=str(path), filename=name,
                extension=path.suffix, status=FileStatus.ANALYZED,
                analysis_outcome="content_verified", analysis_evidence_source=source,
                ai_description="Subject verified from this file", ai_suggested_name=suggestion,
                ai_confidence=0.9, date_modified=modified, date_created=datetime(2026, 9, 28),
                date_exif=exif,
            ))
        db.commit()
        session_id = owner.id
    generate_proposals(session_id=session_id)
    with Session(engine) as db:
        renames = db.query(Proposal).filter(Proposal.proposal_type == ProposalType.RENAME).all()
        by_source = {Path(row.current_value).name: row for row in renames}
        assert len(by_source) == 2
        doc = by_source["report.docx"]
        assert "2018-01-17" not in Path(doc.proposed_value).name
        assert "date" not in doc.reasoning.casefold()
        photo = by_source["IMG_1234.jpg"]
        assert "2020-05-06" in Path(photo.proposed_value).name
        assert "stored EXIF metadata" in photo.reasoning
        assert "capture date unverified" in photo.reasoning


class TestIsUselessStem:
    def test_pure_digits(self):
        assert _is_useless_stem("1")
        assert _is_useless_stem("99")

    def test_camera_defaults(self):
        assert _is_useless_stem("IMG_1234")
        assert _is_useless_stem("DSC0001")
        assert _is_useless_stem("P1010234")

    def test_untitled(self):
        assert _is_useless_stem("untitled")
        assert _is_useless_stem("untitled_1")

    def test_meaningful_stems(self):
        assert not _is_useless_stem("family_photo")
        assert not _is_useless_stem("report_final")
        assert not _is_useless_stem("logo_vector")

    def test_empty_and_whitespace(self):
        assert _is_useless_stem("")
        assert _is_useless_stem("   ")


class TestHygienicStem:
    def test_removes_parens(self):
        assert _hygienic_stem("My Report (Final Draft)") == "My_Report_Final_Draft"

    def test_removes_noisy_chars(self):
        assert _hygienic_stem("file&with#bad!chars") == "file_with_bad_chars"

    def test_normalizes_whitespace(self):
        assert _hygienic_stem("too    many   spaces") == "too_many_spaces"

    def test_no_change_for_clean(self):
        assert _hygienic_stem("normal_file_name") == "normal_file_name"

    def test_preserves_hebrew(self):
        assert _hygienic_stem("תפריט אירוע") == "תפריט_אירוע"


class TestSafe:
    def test_lowercases_and_truncates(self):
        result = _safe("A" * 100)
        assert result == "a" * 60

    def test_strips_special(self):
        assert _safe("hello-world!!") == "hello-world"

    def test_preserves_hebrew(self):
        result = _safe("תנורים")
        assert "תנורים" in result


def test_descriptive_project_script_and_version_identity_are_not_discarded():
    assert not _preserves_descriptive_identity(
        "PROJECTX 2021 Agreement Final", "generic_agreement_document"
    )
    assert _preserves_descriptive_identity(
        "PROJECTX 2021 Agreement Final", "projectx_2021_agreement_final_signed"
    )
    assert not _preserves_descriptive_identity("PROJECTX2", "generic_portrait")
    assert not _preserves_descriptive_identity("PROJECTX", "generic_portrait")
    assert not _preserves_descriptive_identity("A1", "generic_portrait")
    assert not _preserves_descriptive_identity("AB", "generic_portrait")
    assert not _preserves_descriptive_identity("V2", "generic_portrait")
    assert _restore_descriptive_identity("PROJECTX2", "agreement_terms") == "PROJECTX2_agreement_terms"
    assert _preserves_descriptive_identity("1", "white_grooved_surface")
    assert not _preserves_descriptive_identity(
        "תכנית בניין v2", "building_plan"
    )


def test_confident_ai_naming_requires_verified_content_evidence():
    base = {"ai_confidence": 0.95, "analysis_evidence_source": "text"}
    assert _content_verified_for_naming(SimpleNamespace(**base, analysis_outcome="content_verified"))
    assert not _content_verified_for_naming(SimpleNamespace(**base, analysis_outcome="context_only"))
    assert not _content_verified_for_naming(SimpleNamespace(
        ai_confidence=0.95, analysis_outcome="metadata_only", analysis_evidence_source="metadata"
    ))


def test_final_naming_gate_rejects_relation_semantics_and_retains_project_identity(tmp_path):
    from sqlalchemy.orm import Session
    from donedatahoarder.db.session import init_db
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType, UserSession

    engine = init_db(tmp_path / "naming.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        rows = [
            ("untitled.pdf", "portrait_person.pdf", "RelationGroup propagation — borrowed image name"),
            ("PROJECTX2.pdf", "generic_agreement.pdf", "AI-derived descriptive name"),
        ]
        for source, target, reasoning in rows:
            file = File(session_id=owner.id, path=str(tmp_path / source), filename=source,
                        analysis_outcome="content_verified", analysis_evidence_source="text",
                        ai_description="Participation agreement")
            db.add(file)
            db.flush()
            db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                            current_value=file.path, proposed_value=str(tmp_path / target),
                            reasoning=reasoning, status=ProposalStatus.PENDING))
        db.commit()
        sid = owner.id
    removed = _suppress_unsafe_rename_postpasses(sid, set())
    assert removed == {"unsupported_relation_name_transfer": 1}
    with Session(engine) as db:
        proposals = db.query(Proposal).all()
        assert len(proposals) == 1
        assert Path(proposals[0].proposed_value).stem == "PROJECTX2_generic_agreement"


def test_borrowed_same_stem_name_needs_target_content_evidence():
    from donedatahoarder.proposals.namer.core import _borrowed_name_supported
    target = SimpleNamespace(
        path="/synthetic/1.png", analysis_outcome="content_verified",
        analysis_evidence_source="vision", ai_confidence=0.98,
        ai_description="Architectural office floor plan drawing", ai_tags="office, architecture",
    )
    assert not _borrowed_name_supported(target, "white_grooved_surface")
    assert _borrowed_name_supported(target, "architectural_office_floor_plan")
    target.ai_description = "This is not a portrait; it is an office drawing"
    assert not _borrowed_name_supported(target, "portrait")


def test_sessionless_cli_namer_still_enforces_final_safety_gate(tmp_path):
    from sqlalchemy.orm import Session
    from donedatahoarder.db.session import init_db
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType, UserSession
    from donedatahoarder.proposals.namer.core import generate_proposals

    engine = init_db(tmp_path / "sessionless.db")
    with Session(engine) as db:
        for name, reasoning in (
            ("CADFONT.SHX", "AI-derived descriptive name"),
            ("untitled.pdf", "Sibling rename — borrowed image name"),
        ):
            owner = UserSession(root_path=str(tmp_path))
            db.add(owner)
            db.flush()
            file = File(session_id=owner.id, path=str(tmp_path / name), filename=name)
            Path(file.path).write_bytes(b"synthetic")
            db.add(file)
            db.flush()
            db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME,
                            current_value=file.path,
                            proposed_value=str(tmp_path / ("portrait_" + name)),
                            reasoning=reasoning, status=ProposalStatus.PENDING))
        db.commit()
    summary = generate_proposals()
    assert summary["suppressed_unsafe_renames"] == 2
    with Session(engine) as db:
        assert db.query(Proposal).count() == 0
