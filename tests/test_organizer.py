"""
Tests for organizer module — Hebrew transliteration and folder renaming.
"""
import pytest
from types import SimpleNamespace

from donedatahoarder.proposals.organizer import _transliterate_hebrew
from donedatahoarder.proposals.organizer.core import (
    _folder_rename_keeps_identity, _organizer_move_allowed,
    _suppress_unsafe_organizer_proposals,
)


def test_move_requires_project_and_subject_evidence(tmp_path):
    class Unprotected:
        def assess(self, _path):
            return SimpleNamespace(protected=False)

    source = tmp_path / "PROJECTX 2021" / "Presentation-01.png"
    file = SimpleNamespace(
        id=1, path=str(source), ai_description="Phone mockup showing swarming bees",
        ai_tags='["presentation", "bees"]', analysis_outcome="content_verified",
        analysis_evidence_source="vision",
    )
    assert not _organizer_move_allowed(
        file, tmp_path / "PROJECTX 2021" / "projectx-2021-agreement-documents" / source.name,
        tmp_path, Unprotected(), set(),
    )
    assert _organizer_move_allowed(
        file, tmp_path / "PROJECTX 2021" / "presentation_images" / source.name,
        tmp_path, Unprotected(), set(),
    )
    assert not _organizer_move_allowed(
        file, tmp_path / "PROJECTX 2021" / "presentation_agreements" / source.name,
        tmp_path, Unprotected(), set(),
    )
    assert not _organizer_move_allowed(
        file, tmp_path / "Other Project" / "presentation_images" / source.name,
        tmp_path, Unprotected(), set(),
    )
    assert not _organizer_move_allowed(
        file, tmp_path / "PROJECTX 2021" / "presentation_images" / source.name,
        tmp_path, Unprotected(), {1},
    )


def test_folder_rename_preserves_numbered_milestone_identity(tmp_path):
    project = tmp_path / "PROJECTX"
    first = project / "1 Milestone"
    second = project / "2 Milestone"
    assert not _folder_rename_keeps_identity(first, project / "Images")
    assert not _folder_rename_keeps_identity(second, project / "Images")
    assert _folder_rename_keeps_identity(first, project / "1_Milestone_Images")
    assert not _folder_rename_keeps_identity(project / "AB", project / "Images")
    assert not _folder_rename_keeps_identity(project / "A1", project / "Images1")


def test_final_folder_gate_rejects_colliding_generic_milestone_names(tmp_path):
    from sqlalchemy.orm import Session
    from donedatahoarder.db.session import init_db
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType, UserSession

    project = tmp_path / "PROJECTX"
    project.mkdir()
    engine = init_db(tmp_path / "organize.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()
        for number in (1, 2):
            folder = project / f"{number} Milestone"
            folder.mkdir()
            source = folder / "frame.png"
            source.write_bytes(b"synthetic")
            file = File(session_id=owner.id, path=str(source), filename=source.name)
            db.add(file)
            db.flush()
            db.add(Proposal(file_id=file.id, proposal_type=ProposalType.RENAME_FOLDER,
                            current_value=str(folder), proposed_value=str(project / "Images"),
                            status=ProposalStatus.PENDING))
        db.commit()
        sid = owner.id
    summary = _suppress_unsafe_organizer_proposals(sid, str(tmp_path))
    assert summary["rename_folder"] == 2
    with Session(engine) as db:
        assert db.query(Proposal).count() == 0


def test_grouping_moves_reject_repeated_folder_and_orphan_but_keep_real_group(tmp_path):
    from sqlalchemy.orm import Session
    from donedatahoarder.db.session import init_db
    from donedatahoarder.db.models import File, Proposal, ProposalStatus, ProposalType, UserSession

    engine = init_db(tmp_path / "organize-groups.db")
    with Session(engine) as db:
        owner = UserSession(root_path=str(tmp_path))
        db.add(owner)
        db.flush()

        def add_move(parent, filename, destination_folder, reasoning, verified=True):
            folder = tmp_path / parent
            folder.mkdir(exist_ok=True)
            source = folder / filename
            source.write_bytes(b"synthetic")
            file = File(session_id=owner.id, path=str(source), filename=filename,
                        analysis_outcome="content_verified" if verified else "context_only",
                        analysis_evidence_source="text" if verified else "filename_only",
                        ai_description="Invoice record" if verified else None)
            db.add(file)
            db.flush()
            db.add(Proposal(file_id=file.id, proposal_type=ProposalType.MOVE,
                            current_value=str(source),
                            proposed_value=str(folder / destination_folder / filename),
                            reasoning=reasoning, status=ProposalStatus.PENDING))

        cluster = "Cluster move — member of RelationGroup 'invoices' (confidence 0.90)"
        for i in range(3):
            add_move("TEAM", f"team-{i}.txt", "team", cluster)
        add_move("PROJECTA", "one.txt", "invoices", cluster)
        add_move("PROJECTA", "two.txt", "invoices", cluster, verified=False)
        add_move("PROJECTB", "one.txt", "invoices", cluster)
        add_move("PROJECTB", "two.txt", "invoices", cluster)
        add_move("PROJECTC", "solo.txt", "invoices", "Direct file move")
        db.commit()
        sid = owner.id

    summary = _suppress_unsafe_organizer_proposals(sid, str(tmp_path))
    assert summary["move"] == 5
    assert summary["reasons"]["Grouping would repeat the existing folder name"] == 3
    assert summary["reasons"]["Grouping would leave a one-file folder after safety filters"] == 1
    with Session(engine) as db:
        remaining = {
            (file.filename, file.path)
            for _, file in db.query(Proposal, File).join(File, Proposal.file_id == File.id)
        }
    assert len(remaining) == 3
    assert {str(tmp_path / "PROJECTB" / name) for name in ("one.txt", "two.txt")} <= {
        path for _, path in remaining
    }
    assert str(tmp_path / "PROJECTC" / "solo.txt") in {path for _, path in remaining}


class TestHebrewTransliteration:
    """Test Hebrew-to-Latin transliteration for folder names."""

    def test_hebrew_french_word(self):
        """Transliterate Hebrew word (צרפתי = French)."""
        # צרפתי = Tzarfati (French language/nationality)
        result = _transliterate_hebrew("צרפתי")
        # Should contain transliterated Hebrew
        assert result  # Not empty
        assert "tz" in result  # tz for צ (tsade)
        assert "r" in result   # r for ר (resh)

    def test_hebrew_plans_folder(self):
        """Transliterate 'Updated Plans' folder name."""
        # תכניות עדכניות = Plans & Updated
        result = _transliterate_hebrew("תכניות עדכניות")
        # Should have underscore where space was
        assert "_" in result
        # Should have transliterated Hebrew characters
        assert result and not any(c in result for c in "תכניות עדכניות")

    def test_mixed_hebrew_english(self):
        """Transliterate mixed Hebrew and English text."""
        result = _transliterate_hebrew("project_צרפתי")
        # English part preserved, Hebrew transliterated
        assert result.startswith("project_")
        assert len(result) > len("project_")

    def test_hebrew_with_numbers(self):
        """Transliterate Hebrew with digits preserved."""
        result = _transliterate_hebrew("פרויקט3_2013")
        # Digits and underscore preserved
        assert "3" in result
        assert "2013" in result
        assert "_" in result

    def test_latin_passthrough(self):
        """Latin text passes through unchanged."""
        assert _transliterate_hebrew("English_Folder_2024") == "english_folder_2024"

    def test_empty_string(self):
        """Empty string returns empty."""
        assert _transliterate_hebrew("") == ""

    def test_spaces_to_underscores(self):
        """Spaces are converted to underscores."""
        result = _transliterate_hebrew("hello world")
        assert result == "hello_world"

    def test_hyphens_to_underscores(self):
        """Hyphens are converted to underscores."""
        result = _transliterate_hebrew("hello-world")
        assert result == "hello_world"

    def test_hebrew_aleph(self):
        """Test individual Hebrew letter Aleph."""
        assert _transliterate_hebrew("א") == "a"

    def test_hebrew_common_letters(self):
        """Test common Hebrew letters."""
        assert _transliterate_hebrew("ב") == "b"  # Bet
        assert _transliterate_hebrew("ג") == "g"  # Gimel
        assert _transliterate_hebrew("ד") == "d"  # Dalet
        assert _transliterate_hebrew("ר") == "r"  # Resh
        assert _transliterate_hebrew("ש") == "sh"  # Shin

    def test_hebrew_final_forms(self):
        """Test Hebrew final forms (sofit)."""
        assert _transliterate_hebrew("ך") == "k"   # Final Kaph
        assert _transliterate_hebrew("ם") == "m"   # Final Mem
        assert _transliterate_hebrew("ן") == "n"   # Final Nun
        assert _transliterate_hebrew("ף") == "p"   # Final Pe
        assert _transliterate_hebrew("ץ") == "tz"  # Final Tsade

    def test_mixed_hebrew_digits_spaces(self):
        """Complex case: Hebrew + digits + spaces."""
        result = _transliterate_hebrew("תכניות 2024")
        # Space becomes underscore
        assert "_" in result
        # Digits preserved
        assert "2024" in result
        # Result is not empty and doesn't contain Hebrew
        assert result and not any(c in result for c in "תכניות")

    def test_case_normalization(self):
        """Output should be lowercase."""
        result = _transliterate_hebrew("HELLO")
        assert result == result.lower()

    def test_tzarfati_folder_name(self):
        """Test realistic folder name: Tzarfati (designer folder)."""
        # This represents a real folder name from the test
        result = _transliterate_hebrew("צרפתי")
        # Just verify it's transliterated and usable as a folder name
        assert result  # Not empty
        assert not any(c in result for c in "צרפתי")  # No Hebrew chars left
        assert all(c.isalnum() or c == "_" for c in result)  # Valid folder chars

    def test_plans_folder_name(self):
        """Test realistic folder name: תכניות עדכניות (Updated Plans)."""
        result = _transliterate_hebrew("תכניות עדכניות")
        # Verify it's transliterated
        assert result
        assert not any(c in result for c in "תכניות עדכניות")  # No Hebrew chars
        assert all(c.isalnum() or c == "_" for c in result)  # Valid folder chars
        assert "_" in result  # Space preserved as underscore
