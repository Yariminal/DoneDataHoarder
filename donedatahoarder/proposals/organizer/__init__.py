"""
Folder organizer — uses LLM to suggest folder-level reorganization.

Two-phase approach:
1. Build a compact folder summary tree from analyzed file metadata
2. Ask the LLM to propose MOVE operations for better organization

This package used to be a single module (organizer.py). Every name that was
importable from `donedatahoarder.proposals.organizer` is re-exported here so
existing import paths keep working.
"""
from .backstops import (
    _GENERIC_FOLDER_PATTERNS,
    _backstop_generic_folders,
    _backstop_mojibake_folders,
    _backstop_nonlatin_folders,
    _derive_english_folder_name,
    _emit_relation_group_moves,
    _folder_content_label,
    _is_generic_folder_name,
    _propagate_moves_to_skipped_siblings,
)
from .core import (
    generate_reorg_proposals,
    generate_reorg_proposals_with_progress,
    logger,
)
from .prompts import REORG_SYSTEM_PROMPT
from .text_utils import (
    _BASIC_LETTER_RANGES,
    _HEBREW_TRANS,
    _MOJIBAKE_RIGHT_DECODINGS,
    _MOJIBAKE_WRONG_DECODINGS,
    _SCRIPT_RANGES,
    _case_alternation_ratio,
    _folder_is_mojibake,
    _is_basic_letter,
    _is_recognised_script,
    _normalize_folder_name,
    _recover_mojibake,
    _score_mojibake_recovery,
    _transliterate_hebrew,
)
from .tree import (
    _EXT_CATEGORY,
    FolderSummary,
    _file_category,
    _format_tree_for_prompt,
    _human_size,
    build_folder_tree,
)
