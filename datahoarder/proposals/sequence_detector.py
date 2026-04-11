"""
Sequence detector — identifies numbered/sequential files and preserves their patterns.

For example, "notes_1.pdf", "notes_2.pdf", "notes_10.pdf" are detected as a sequence
with base name "notes", numbers [1, 2, 10], and separator "_".
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class SequenceInfo:
    """Metadata about a detected sequence."""
    base_name: str              # Common prefix before number (e.g., "notes")
    numbers_found: List[int]    # List of numbers in sequence (e.g., [1, 2, 10])
    separator: str              # Separator before number (e.g., "_", "-", " ", "")
    is_padded: bool             # Whether numbers are zero-padded (01, 02, 001, etc.)
    padding_width: int          # Width if padded (e.g., 2 for "01", 3 for "001")

    def format_number(self, num: int) -> str:
        """Format a number in this sequence's style."""
        if self.is_padded:
            return str(num).zfill(self.padding_width)
        return str(num)


def detect_sequences(parent_dir: Path, filename: str) -> Optional[SequenceInfo]:
    """
    Detect if a file is part of a numbered sequence.

    Args:
        parent_dir: Directory containing the file
        filename: The filename to analyze (without path)

    Returns:
        SequenceInfo if a sequence is detected, None otherwise.

    Example:
        If parent_dir contains ["notes_1.pdf", "notes_2.pdf", "notes_10.pdf"],
        detect_sequences(parent_dir, "notes_1.pdf") returns:
            SequenceInfo(base_name="notes", numbers_found=[1, 2, 10],
                        separator="_", is_padded=False, padding_width=0)
    """
    try:
        # Get all files in parent directory
        if not parent_dir.exists():
            return None

        siblings = [f.name for f in parent_dir.iterdir() if f.is_file()]
        if not siblings:
            return None

        # Extract the extension and base name
        stem, ext = Path(filename).stem, Path(filename).suffix

        # Try different regex patterns to find numbers and separators
        # Patterns: base_name{separator}{number} or base_name{number}

        # Look for patterns like: text_1, text-1, text 1, text1
        # More specific patterns first
        patterns = [
            (r"^(.+?)_(\d+)$", "_"),      # "name_1", "name_01"
            (r"^(.+?)-(\d+)$", "-"),      # "name-1", "name-01"
            (r"^(.+?)\s(\d+)$", " "),     # "name 1", "name 01"
            (r"^(.+?)(\d+)$", ""),        # "name1", "name01" (no separator)
        ]

        # Try each pattern
        for pattern, separator in patterns:
            match = re.match(pattern, stem)
            if not match:
                continue

            base_name = match.group(1).strip()
            our_number_str = match.group(2)

            # Verify base name is meaningful (not just a few chars)
            if len(base_name) < 2:
                continue

            # Check if other files match this pattern
            our_number = int(our_number_str)
            numbers_in_sequence = [our_number]
            is_padded = our_number_str[0] == '0' and len(our_number_str) > 1
            padding_width = len(our_number_str) if is_padded else 0

            # Find other files with same pattern
            for sibling in siblings:
                if sibling == filename:
                    continue

                sibling_stem = Path(sibling).stem
                sibling_match = re.match(pattern, sibling_stem)
                if not sibling_match:
                    continue

                sibling_base = sibling_match.group(1).strip()
                sibling_number_str = sibling_match.group(2)

                # Must have same base name
                if sibling_base != base_name:
                    continue

                sibling_number = int(sibling_number_str)
                numbers_in_sequence.append(sibling_number)

                # Check padding consistency
                sibling_padded = sibling_number_str[0] == '0' and len(sibling_number_str) > 1
                if sibling_padded:
                    padding_width = max(padding_width, len(sibling_number_str))

            # Only consider it a sequence if at least 2 files match
            if len(numbers_in_sequence) >= 2:
                numbers_in_sequence.sort()
                return SequenceInfo(
                    base_name=base_name,
                    numbers_found=numbers_in_sequence,
                    separator=separator,
                    is_padded=is_padded,
                    padding_width=padding_width or 1,
                )

        return None

    except Exception:
        # If any error occurs during detection, just return None (not a sequence)
        return None


