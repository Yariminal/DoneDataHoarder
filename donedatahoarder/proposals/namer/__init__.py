"""
Proposal generator — builds rename / move / tag proposals for analyzed files.

For every ANALYZED file it creates one or more Proposal records:
- RENAME:  new filename based on date + AI description
- MOVE:    suggested destination folder (future)
- ADD_TAGS: metadata tags to embed

Naming conventions:
  Photos/Videos:   YYYY-MM-DD_HH-MM-SS_<description>.<ext>
                   YYYY-MM-DD_<description>.<ext>  (if no time component)
  Documents:       YYYY-MM-DD_<description>.<ext>
                   <description>.<ext>  (if no meaningful date)
  Other:           <description>.<ext>

This package used to be a single module (namer.py). Every name that was
importable from `donedatahoarder.proposals.namer` is re-exported here so
existing import paths keep working.
"""
from .core import (
    generate_proposals,
    generate_proposals_with_progress,
)
from .llm import (
    _translation_cache,
    translate_filename,
)
from .naming import (
    DOC_EXTENSIONS,
    MEDIA_EXTENSIONS,
    _ECHO_STOPWORDS,
    _GENERIC_STEM_TOKENS,
    _ORIGINAL_PREFIX_RE,
    _build_echo_blocklist,
    _content_type_prefix,
    _date_prefix,
    _deduplicate_stem_words,
    _ensure_prefix,
    _extract_distinguishing_prefix,
    _folder_context_fallback,
    _get_hygiene_config,
    _get_useless_patterns,
    _hygienic_stem,
    _is_meaningful_date,
    _is_useless_stem,
    _month_prefix,
    _needs_hygiene,
    _resolve_collision,
    _safe,
    _strip_context_echo,
    build_new_name,
)
from .postpass import (
    _disambiguate_generic_stems_in_dir,
    _flag_near_duplicate_proposals,
    _generate_fallback_for_useless_stems,
    _generate_hygiene_fallback,
    _normalize_spelling_in_proposals,
    _propagate_renames_to_siblings,
    _propagate_renames_via_relation_groups,
)
