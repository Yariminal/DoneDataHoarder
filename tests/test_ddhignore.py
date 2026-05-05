"""
Tests for .ddhignore file parsing and pattern matching.
"""
import tempfile
from pathlib import Path

import pytest

from donedatahoarder.core.ignore import DdhIgnore, load_ddhignore


class TestDdhIgnorePatterns:
    """Test .ddhignore pattern matching logic."""

    def test_empty_ignore_file(self):
        """Empty ignore list matches nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("")
            ignore = load_ddhignore(root)
            assert not ignore.should_ignore(root / "file.txt")
            assert not ignore.should_ignore(root / "subdir", is_dir=True)

    def test_no_ignore_file(self):
        """Missing .ddhignore file is treated as empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ignore = load_ddhignore(root)
            assert not ignore.should_ignore(root / "file.txt")

    def test_simple_filename_pattern(self):
        """Match files by exact name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("backup.zip\n")
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "backup.zip")
            assert not ignore.should_ignore(root / "important.zip")

    def test_wildcard_extension(self):
        """Match all files with a given extension."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("*.tmp\n*.log\n")
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "file.tmp")
            assert ignore.should_ignore(root / "debug.log")
            assert not ignore.should_ignore(root / "file.txt")

    def test_directory_pattern(self):
        """Match directories by name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("node_modules\n")
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "node_modules", is_dir=True)
            assert ignore.should_ignore(root / "node_modules" / "package", is_dir=True)
            assert not ignore.should_ignore(root / "file.txt")

    def test_trailing_slash_directory_only(self):
        """Trailing slash should only match directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("temp/\n")
            ignore = load_ddhignore(root)
            # Should match directory
            assert ignore.should_ignore(root / "temp", is_dir=True)
            # Should NOT match file with same name
            assert not ignore.should_ignore(root / "temp", is_dir=False)

    def test_leading_slash_root_only(self):
        """Leading slash should only match at root level."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("/secret\n")
            ignore = load_ddhignore(root)
            # Should match at root
            assert ignore.should_ignore(root / "secret")
            # Should NOT match in subdirectory
            assert not ignore.should_ignore(root / "subdir" / "secret")

    def test_negation_pattern(self):
        """Exclamation mark negates a pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("*.tmp\n!important.tmp\n")
            ignore = load_ddhignore(root)
            # Matched by *.tmp
            assert ignore.should_ignore(root / "file.tmp")
            # Negated by !important.tmp
            assert not ignore.should_ignore(root / "important.tmp")

    def test_comments_ignored(self):
        """Lines starting with # should be ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text(
                "# This is a comment\n"
                "*.tmp\n"
                "# Another comment\n"
                "*.log\n"
            )
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "file.tmp")
            assert ignore.should_ignore(root / "debug.log")

    def test_empty_lines_ignored(self):
        """Empty lines should be ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text(
                "*.tmp\n"
                "\n"
                "\n"
                "*.log\n"
            )
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "file.tmp")
            assert ignore.should_ignore(root / "debug.log")

    def test_glob_patterns_nested(self):
        """Match files in nested directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("build/*\n")
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "build" / "output.o")
            assert ignore.should_ignore(root / "build" / "subdir" / "file.o")
            assert not ignore.should_ignore(root / "file.o")

    def test_wildcard_question_mark(self):
        """? should match single character."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("file?.txt\n")
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "file1.txt")
            assert ignore.should_ignore(root / "fileA.txt")
            assert not ignore.should_ignore(root / "file.txt")
            assert not ignore.should_ignore(root / "file12.txt")

    def test_multiple_patterns_last_wins(self):
        """Later patterns override earlier ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text(
                "*.tmp\n"  # Ignore all .tmp files
                "!keep.tmp\n"  # Except keep.tmp
                "delete.tmp\n"  # Actually, ignore delete.tmp again
            )
            ignore = load_ddhignore(root)
            assert ignore.should_ignore(root / "file.tmp")
            assert not ignore.should_ignore(root / "keep.tmp")
            assert ignore.should_ignore(root / "delete.tmp")

    def test_relative_path_handling(self):
        """Paths should be properly normalized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("*.tmp\n")
            ignore = load_ddhignore(root)
            # Absolute path
            assert ignore.should_ignore(root / "file.tmp")
            # Path outside root should not match
            other_root = Path(tmpdir) / ".." / "other"
            assert not ignore.should_ignore(other_root / "file.tmp")


class TestDdhIgnoreIntegration:
    """Test integration with real file structures."""

    def test_real_directory_scan(self):
        """Test with actual directory structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Create file structure
            (root / "src").mkdir()
            (root / "src" / "code.py").touch()
            (root / "build").mkdir()
            (root / "build" / "output.o").touch()
            (root / "temp").mkdir()
            (root / "temp" / "file.tmp").touch()
            (root / "important.tmp").touch()

            # Create .ddhignore
            (root / ".ddhignore").write_text(
                "build/\n"
                "temp/\n"
                "*.tmp\n"
                "!important.tmp\n"
            )

            ignore = load_ddhignore(root)

            # Should ignore directories
            assert ignore.should_ignore(root / "build", is_dir=True)
            assert ignore.should_ignore(root / "temp", is_dir=True)

            # Should ignore temp files except important.tmp
            assert ignore.should_ignore(root / "temp" / "file.tmp")
            assert not ignore.should_ignore(root / "important.tmp")

            # Should not ignore source code
            assert not ignore.should_ignore(root / "src" / "code.py")

    def test_nested_directories(self):
        """Test pattern matching in nested directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".ddhignore").write_text("**/node_modules\n")
            ignore = load_ddhignore(root)

            # Should match at any level
            assert ignore.should_ignore(root / "node_modules", is_dir=True)
            assert ignore.should_ignore(root / "project" / "node_modules", is_dir=True)
            assert ignore.should_ignore(root / "a" / "b" / "c" / "node_modules", is_dir=True)
