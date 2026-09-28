"""Read-only collection sizing. This walks directory entries but never reads bytes."""
from __future__ import annotations

import shutil
import os
import time
from collections import Counter
from pathlib import Path

from donedatahoarder.core.scanner import walk_files, _is_link_or_reparse
from donedatahoarder.proposals.sequence_identity import numbered_frame_identity


_AI_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".pdf", ".txt", ".md", ".csv", ".json", ".xml", ".html",
    ".docx", ".pptx", ".xlsx", ".odt", ".rtf", ".py", ".js", ".ts",
    ".css", ".svg", ".mp4", ".mov", ".mkv", ".avi", ".obj", ".fbx",
    ".blend", ".zip", ".7z", ".rar",
}
MIB = 1024 * 1024


def estimate_collection(root: Path, *, mode: str = "full",
                        sequence_sample_stride: int = 0,
                        model_seconds_per_file: float = 5.0,
                        extra_skip_dirs: set[str] | None = None) -> dict:
    """Estimate work from names/stats, with explicit uncertainty and no content IO.

    `mode` is full, representative, or metadata_only. Representative applies
    only to numbered visual frames and estimates saved calls conservatively.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"Directory does not exist: {root}")
    if _is_link_or_reparse(root):
        raise ValueError("Collection root is a symlink, junction, or unreadable")
    root = root.resolve()
    if mode not in {"full", "representative", "metadata_only"}:
        raise ValueError("mode must be full, representative or metadata_only")
    if sequence_sample_stride < 0 or model_seconds_per_file <= 0:
        raise ValueError("stride must be non-negative and seconds positive")
    if mode == "representative" and sequence_sample_stride < 2:
        raise ValueError("representative mode requires stride >= 2")

    started = time.perf_counter()
    files = 0
    indexed_logical_bytes = 0
    ai_candidates = 0
    numbered_visual_candidates = 0
    inaccessible = 0
    unreadable_directories: list[str] = []
    extensions: Counter[str] = Counter()
    for path in walk_files(root, extra_skip_dirs):
        try:
            stat = path.stat()
        except OSError:
            inaccessible += 1
            continue
        files += 1
        indexed_logical_bytes += stat.st_size
        ext = path.suffix.lower()
        extensions[ext or "[none]"] += 1
        ai_candidates += ext in _AI_EXTENSIONS
        if ext in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
            numbered_visual_candidates += numbered_frame_identity(path) is not None

    estimated_sampled = 0
    if mode == "representative":
        # Upper bound: the actual analyzer samples only confirmed adjacent
        # numbered images, so fewer may be sampled than this estimate.
        estimated_sampled = numbered_visual_candidates * (
            sequence_sample_stride - 1) // sequence_sample_stride
    ai_calls_upper = 0 if mode == "metadata_only" else ai_candidates
    ai_calls_lower = max(0, ai_calls_upper - estimated_sampled)
    # Count every real file in the collection for copy-space estimates,
    # including paths intentionally excluded from the app index.
    full_files = 0
    full_bytes = 0
    def _walk_error(exc: OSError) -> None:
        unreadable_directories.append(str(getattr(exc, "filename", "unknown")))

    if not _is_link_or_reparse(root):
        for dirpath, dirs, names in os.walk(root, followlinks=False,
                                            onerror=_walk_error):
            base = Path(dirpath)
            dirs[:] = [name for name in dirs if not _is_link_or_reparse(base / name)]
            for name in names:
                path = base / name
                if _is_link_or_reparse(path):
                    continue
                try:
                    full_bytes += path.stat().st_size
                    full_files += 1
                except OSError:
                    inaccessible += 1
    # Enrichment reads indexed bytes once. With cache enabled analysis reads
    # each indexed eligible file before inference and again before admission.
    # Use three full indexed-byte passes as a conservative hash-I/O bound.
    hash_read_seconds = {
        "optimistic": round(indexed_logical_bytes / (200 * MIB)),
        "conservative": round(indexed_logical_bytes * 3 / (50 * MIB)),
    }
    # Index row + index/WAL overhead is workload-dependent; reserve a range.
    db_bytes_low = files * 2_048
    db_bytes_high = files * 8_192
    free_bytes = shutil.disk_usage(root).free
    complete = inaccessible == 0 and not unreadable_directories
    return {
        "root": str(root), "mode": mode,
        "sequence_sample_stride": sequence_sample_stride,
        "files": files, "logical_bytes": full_bytes,
        "indexed_logical_bytes": indexed_logical_bytes,
        "full_collection_files": full_files,
        "excluded_files": max(0, full_files - files),
        "physically_hashed_bytes": 0,
        "inaccessible_entries": inaccessible,
        "unreadable_directories": unreadable_directories[:20],
        "size_estimate_complete": complete,
        "ai_candidate_files_upper": ai_candidates,
        "numbered_visual_candidates_upper": numbered_visual_candidates,
        "estimated_sampled_upper": estimated_sampled,
        "estimated_ai_calls_range": [ai_calls_lower, ai_calls_upper],
        "estimated_ai_seconds_range": [
            round(ai_calls_lower * model_seconds_per_file * 0.5),
            round(ai_calls_upper * model_seconds_per_file * 2.0),
        ],
        "estimated_hash_seconds_range": hash_read_seconds,
        "estimated_db_bytes_range": [db_bytes_low, db_bytes_high],
        "free_bytes_on_root_volume": free_bytes,
        "copy_plus_index_bytes_upper": full_bytes + db_bytes_high if complete else None,
        "top_extensions": extensions.most_common(12),
        "stat_walk_seconds": round(time.perf_counter() - started, 3),
        "estimate_note": (
            "Stat-only preflight. Copy budget includes excluded regular files "
            "when size_estimate_complete is true; otherwise logical_bytes is "
            "only a measured lower bound. "
            "symlinks and junctions are excluded. AI eligibility and sampling "
            "are estimates; hash read range includes one enrichment pass and "
            "up to two cache validation passes at 50-200 MiB/s. Cache hits "
            "and provider variability are unknown until the run."
        ),
    }
