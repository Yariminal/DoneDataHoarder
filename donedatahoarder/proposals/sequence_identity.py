"""Cheap, conservative numbered visual-frame recognition shared by phases."""
from __future__ import annotations

import re
from pathlib import Path


_PREFIXED = re.compile(r"^(.{2,}?)([_ -])(\d{2,8})$", re.ASCII)
_BARE_PADDED = re.compile(r"^(0\d{2,7})$", re.ASCII)
_VISUAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


def numbered_frame_identity(path: Path) -> tuple[str, int, int] | None:
    """Return (family stem prefix, frame number, width) for visual filenames."""
    path = Path(path)
    if path.suffix.lower() not in _VISUAL_EXTENSIONS:
        return None
    stem = path.stem
    prefixed = _PREFIXED.fullmatch(stem)
    if prefixed:
        digits = prefixed.group(3)
        return prefixed.group(1) + prefixed.group(2), int(digits), len(digits)
    bare = _BARE_PADDED.fullmatch(stem)
    if bare:
        digits = bare.group(1)
        return "", int(digits), len(digits)
    return None


def is_confirmed_frame(path: Path, *, min_neighbors: int = 3) -> bool:
    """Require several adjacent same-family files; two similarly named photos do not qualify."""
    identity = numbered_frame_identity(path)
    if identity is None or min_neighbors < 1:
        return False
    prefix, number, width = identity
    path = Path(path)
    present = {0}
    for offset in (-3, -2, -1, 1, 2, 3):
        adjacent = number + offset
        if adjacent < 0:
            continue
        neighbor = path.with_name(f"{prefix}{adjacent:0{width}d}{path.suffix}")
        if neighbor.is_file():
            present.add(offset)
    return any(all(offset in present for offset in range(start, start + min_neighbors + 1))
               for start in range(-min_neighbors, 1))
