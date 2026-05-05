"""
.ddhignore file parser — gitignore-style pattern matching for DoneDataHoarder.

Similar to .gitignore, a .ddhignore file in the root directory specifies files
and directories that should be skipped during scanning.

Pattern syntax:
    # Comment (ignored)

    # Glob patterns
    *.tmp                   Skip all .tmp files
    temp/                   Skip 'temp' directory and all contents
    build/*                 Skip everything inside 'build' directory
    **/node_modules         Skip 'node_modules' at any depth

    # Negation (include even if matched above)
    !important.tmp          Don't skip this specific file

    # Trailing slash = directory only
    backups/                Only match directories named 'backups'

    # Leading slash = from root only
    /secret                 Only match 'secret' at root level
"""
import fnmatch
from pathlib import Path
from typing import Optional


class DdhIgnore:
    """Represents a .ddhignore file with gitignore-style pattern matching."""

    def __init__(self, root: Path):
        """
        Initialize a DdhIgnore parser for a given root directory.

        Args:
            root: The root directory to search for .ddhignore file
        """
        self.root = root.resolve()
        self.patterns: list[tuple[str, bool]] = []  # (pattern, is_negation)
        self._load_ignore_file()

    def _load_ignore_file(self) -> None:
        """Load patterns from .ddhignore file if it exists."""
        ignore_path = self.root / ".ddhignore"
        if not ignore_path.exists():
            return

        try:
            with open(ignore_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    # Strip whitespace
                    line = line.rstrip("\r\n")
                    # Skip empty lines and comments
                    if not line.strip() or line.strip().startswith("#"):
                        continue

                    is_negation = False
                    pattern = line.strip()

                    # Handle negation (!)
                    if pattern.startswith("!"):
                        is_negation = True
                        pattern = pattern[1:].lstrip()

                    if not pattern:
                        continue

                    self.patterns.append((pattern, is_negation))
        except (IOError, OSError):
            # If we can't read the file, just proceed without ignore patterns
            pass

    def should_ignore(self, path: Path, is_dir: bool = False) -> bool:
        """
        Determine if a path should be ignored based on .ddhignore patterns.

        Args:
            path: The path to check (can be relative to root or absolute)
            is_dir: Whether the path is a directory

        Returns:
            True if the path should be ignored, False otherwise
        """
        if not self.patterns:
            return False

        # Normalize path relative to root
        try:
            rel_path = path.resolve().relative_to(self.root)
        except ValueError:
            # Path is outside root, don't ignore
            return False

        # Convert to forward slashes for pattern matching
        path_str = rel_path.as_posix()

        # Track if matched (ignore patterns override each other in order)
        ignored = False

        for pattern, is_negation in self.patterns:
            if self._matches_pattern(path_str, pattern, is_dir):
                ignored = not is_negation

        return ignored

    @staticmethod
    def _matches_pattern(path_str: str, pattern: str, is_dir: bool) -> bool:
        """
        Check if a path matches a gitignore pattern.

        Supports:
        - Wildcards: *, ?, [abc]
        - Directory globbing: **/
        - Trailing slash: directory-only
        - Leading slash: root-only match
        """
        # Handle trailing slash (directory-only pattern)
        dir_only = False
        if pattern.endswith("/"):
            dir_only = True
            if not is_dir:
                return False
            pattern = pattern.rstrip("/")

        # Handle leading slash (root-only match)
        root_only = False
        if pattern.startswith("/"):
            root_only = True
            pattern = pattern[1:]

        # Handle ** pattern (match at any depth)
        if pattern.startswith("**/"):
            # Pattern like **/node_modules should match at any level
            sub_pattern = pattern[3:]  # Remove **/
            # Try matching as both directory name and full path
            parts = path_str.split("/")
            for i, part in enumerate(parts):
                if fnmatch.fnmatch(part, sub_pattern):
                    return True
            # Also try matching the full path with wildcards
            if fnmatch.fnmatch(path_str, pattern.replace("**/", "*")):
                return True

        if "**" in pattern:
            # Simple ** handling: replace with * for matching
            pattern = pattern.replace("**", "*")

        # If root-only, only match if path has no directory component
        if root_only:
            if "/" in path_str:
                return False
            return fnmatch.fnmatch(path_str, pattern)

        # Try to match the full path
        if fnmatch.fnmatch(path_str, pattern):
            return True

        # Try to match just the filename (for patterns like *.log)
        if "/" not in pattern:
            filename = path_str.split("/")[-1]
            if fnmatch.fnmatch(filename, pattern):
                return True

        # Try to match as a directory name component (for node_modules style patterns)
        # This allows "node_modules" pattern to match node_modules/package
        if not dir_only and "/" not in pattern:
            parts = path_str.split("/")
            # Check if any path component matches (e.g., "node_modules" in "node_modules/package")
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True

        return False


def load_ddhignore(root: Path) -> DdhIgnore:
    """
    Load .ddhignore patterns from the root directory.

    Args:
        root: The root directory to search for .ddhignore

    Returns:
        A DdhIgnore instance (empty if no .ddhignore file found)
    """
    return DdhIgnore(root)
