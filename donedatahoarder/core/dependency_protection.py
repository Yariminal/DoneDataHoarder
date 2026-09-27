"""Conservative guards for files whose relative references would break on move.

This is an execution-time index, not a file rewriter. Unresolved references
keep their source protected; known referenced resources are protected too.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


_CAD_MODELS = {".dwg", ".dxf", ".dwt", ".dws"}
_CAD_RESOURCES = {".shx", ".ctb", ".stb", ".ttf", ".otf", ".fon", ".pat", ".lin"}
_CAD_SIDECARS = {".bak", ".sv$", ".ac$", ".dwl", ".dwl2"}
_TEXTURES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".tga", ".dds"}
_OPAQUE_3D = {".max", ".3ds", ".3dm"}
_ETRANSMIT_RESOURCES = (_CAD_MODELS | _CAD_RESOURCES | _CAD_SIDECARS
                        | _TEXTURES | {".fmp", ".pc3", ".pmp"})
_ETRANSMIT_REFERENCE = re.compile(
    r"^\s*(.+\.(?:dwg|dxf|dwt|dws|bak|sv\$|ac\$|dwl2?|shx|ctb|stb|ttf|otf|fon|"
    r"pat|lin|png|jpe?g|tiff?|bmp|tga|dds|fmp|pc3|pmp))\s*$", re.I,
)
_MTL_MAP = re.compile(r"^(?:map_[\w]+|bump|disp|decal|refl)\s+(.+)$", re.I)


@dataclass(frozen=True)
class ProtectionDecision:
    protected: bool
    reason: str | None = None
    evidence: tuple[str, ...] = ()


class ProtectionIndex:
    """Snapshot of recognized relative-reference bundles beneath one root."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._reasons: dict[Path, set[str]] = {}
        self._build()

    def _add(self, path: Path, reason: str) -> None:
        resolved = path.resolve()
        if resolved.is_relative_to(self.root):
            self._reasons.setdefault(resolved, set()).add(reason)

    def _safe_reference_path(self, source: Path, raw: str) -> Path | None:
        """Resolve an in-root relative reference without probing outside it."""
        name = raw.strip().strip('"').replace("\\", "/")
        if not name or re.match(r"^[A-Za-z]:", name) or name.startswith("/"):
            return None
        # Normalize parent traversal lexically before touching the filesystem.
        lexical = Path(os.path.normpath(str(source.parent / name)))
        if not lexical.is_relative_to(self.root):
            return None
        # A symlink can point outside the collection, even when its spelling
        # looks local. Reject it without following its target.
        current = lexical
        while current != self.root:
            if current.is_symlink():
                return None
            current = current.parent
        return lexical.resolve()

    def _referenced(self, source: Path, raw: str, kind: str) -> None:
        """Protect an existing reference, or its source if parsing is uncertain."""
        raw = raw.strip().strip('"')
        if not raw:
            self._add(source, f"Unresolved {kind} reference")
            return
        target = self._safe_reference_path(source, raw)
        self._add(source, f"Contains relative {kind} reference: {raw}")
        if target is not None and target.is_file():
            self._add(target, f"Referenced by {source.name}: {raw}")
        else:
            self._add(source, f"Unresolved {kind} reference: {raw}")
            suffixes = {".mtl"} if kind == "OBJ material" else _TEXTURES
            self._protect_potential_resources(source, suffixes, kind)

    def _protect_potential_resources(self, source: Path, suffixes: set[str], kind: str) -> None:
        for sibling in source.parent.iterdir():
            if sibling.is_file() and sibling.suffix.lower() in suffixes:
                self._add(sibling, f"Potential unresolved {kind} dependency of {source.name}")

    def _read_lines(self, path: Path):
        try:
            # References commonly appear in an OBJ header. Stream a bounded
            # prefix instead of skipping large meshes or reading them whole.
            lines = []
            consumed = 0
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                while True:
                    line = stream.readline(64 * 1024)
                    if not line:
                        break
                    consumed += len(line)
                    if consumed > 8_000_000:
                        self._add(path, "Model references beyond inspected prefix are unknown")
                        self._protect_potential_resources(path, {".mtl"} | _TEXTURES,
                                                          "model")
                        break
                    lines.append(line)
            return lines
        except OSError:
            self._add(path, "Reference-bearing model could not be inspected")
            return []

    @staticmethod
    def _head(path: Path, limit: int = 8192) -> bytes:
        try:
            with path.open("rb") as stream:
                return stream.read(limit)
        except OSError:
            return b""

    def _protect_etransmit(self, report: Path) -> None:
        """Keep an AutoCAD inventory and its known or uncertain local assets."""
        self._add(report, "AutoCAD eTransmit dependency inventory")
        unresolved = False
        # A report may contain legacy-encoded names. Decode losslessly enough
        # to identify ASCII extensions, then treat unresolvable paths as
        # incomplete evidence rather than guessing a replacement filename.
        for line in self._read_lines(report):
            match = _ETRANSMIT_REFERENCE.match(line)
            if not match:
                continue
            raw = match.group(1).strip().strip('"')
            target = self._safe_reference_path(report, raw)
            if target is not None and target.is_file():
                self._add(target, f"Listed by AutoCAD eTransmit report {report.name}")
            else:
                unresolved = True
        if unresolved:
            # Do not assume that mangled Hebrew paths or missing external
            # resources enumerate the bundle completely. This is bounded to
            # the report's subtree and recognized CAD/raster/font formats.
            for candidate in report.parent.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in _ETRANSMIT_RESOURCES:
                    self._add(candidate, f"Possible unresolved eTransmit dependency of {report.name}")

    def _build(self) -> None:
        # Stream the tree; retain only directories with recognized CAD
        # members, rather than buffering every path in a large collection.
        cad_by_dir: dict[Path, tuple[list[Path], list[Path], list[Path]]] = {}
        for path in self.root.rglob("*"):
            if path.is_dir():
                if path.suffix.lower() == ".fbm":
                    self._add(path, "Media bundle directory (.fbm)")
                continue
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext in _CAD_MODELS or ext in _CAD_RESOURCES or ext in _CAD_SIDECARS:
                models, resources, sidecars = cad_by_dir.setdefault(path.parent, ([], [], []))
                (models if ext in _CAD_MODELS else resources if ext in _CAD_RESOURCES
                 else sidecars).append(path)
            if ext in _CAD_MODELS:
                self._add(path, "CAD source may contain opaque external resource references")
            if ext == ".fbx":
                self._add(path, "FBX source may contain opaque external resource references")
            if any(parent.suffix.lower() == ".fbm" for parent in path.parents
                   if parent.is_relative_to(self.root)):
                self._add(path, "Media asset inside .fbm bundle")
            if ext in _OPAQUE_3D:
                self._add(path, "3D source may contain opaque relative resource references")
                self._protect_potential_resources(path, _TEXTURES | {".mtl"}, "3D model")
            if ext in {".shx", ".ctb", ".stb", ".fon"}:
                # These are addressable CAD/font resources even when the
                # referencing drawing is outside the indexed tree.
                self._add(path, "CAD/font resource path may be referenced externally")
            if ext == ".lst":
                head = self._head(path)
                if head.lstrip().startswith(b"%!Adobe-FontList"):
                    self._add(path, "Adobe font directory and outline filename map")
            elif ext == ".xml":
                head = self._head(path).lower()
                if (path.name.casefold() == "mstnfontconfig.xml"
                        and b"<fontconfig" in head) or (
                        b"<fontconfig" in head and b"<defaultshxfont" in head):
                    self._add(path, "Vendor CAD font configuration")
            elif ext == ".txt":
                head = self._head(path).lower()
                if (b"transmittal report:" in head
                        and b"created by autocad etransmit" in head):
                    self._protect_etransmit(path)
            if ext == ".obj":
                for line in self._read_lines(path):
                    stripped = line.strip()
                    parts = stripped.split(None, 1)
                    if parts and parts[0].lower() == "mtllib":
                        raw = parts[1].strip() if len(parts) > 1 else ""
                        # Most exporters write one path, including paths with spaces.
                        # If that path is absent, try the OBJ multi-library syntax.
                        candidate = self._safe_reference_path(path, raw)
                        if candidate is not None and candidate.is_file():
                            self._referenced(path, raw, "OBJ material")
                        else:
                            try:
                                names = shlex.split(raw, posix=False)
                            except ValueError:
                                names = [raw]
                            for name in ([raw] if candidate is None else names):
                                self._referenced(path, name, "OBJ material")
            elif ext == ".mtl":
                for line in self._read_lines(path):
                    match = _MTL_MAP.match(line.strip())
                    if not match:
                        continue
                    raw = match.group(1).strip()
                    # MTL texture options have variable arity; use a known
                    # existing suffix candidate, otherwise keep the MTL safe.
                    candidate = self._safe_reference_path(path, raw)
                    if candidate is not None and candidate.is_file():
                        self._referenced(path, raw, "MTL texture")
                    else:
                        try:
                            tokens = shlex.split(raw, posix=False)
                        except ValueError:
                            tokens = [raw]
                        candidates = [" ".join(tokens[i:]) for i in range(len(tokens))
                                      if (target := self._safe_reference_path(
                                          path, " ".join(tokens[i:]))) is not None
                                      and target.is_file()]
                        if candidates:
                            self._referenced(path, candidates[0], "MTL texture")
                        else:
                            self._add(path, f"Unresolved MTL texture reference: {raw}")
                            self._protect_potential_resources(path, _TEXTURES, "MTL texture")
            elif ext == ".fbx":
                folder = path.parent / f"{path.stem}.fbm"
                if folder.is_dir():
                    self._add(path, f"FBX media bundle: {folder.name}")
                    self._add(folder, f"FBX media bundle: {path.name}")

        for directory, (models, resources, sidecars) in cad_by_dir.items():
            if models and resources:
                for item in models + resources:
                    self._add(item, f"CAD model/resource bundle in {directory.name}")
            # AutoCAD backups, autosaves, and lock files share a drawing stem.
            # Protect only those with a colocated CAD source; unrelated .bak
            # files should remain reviewable.
            model_stems = {item.stem.casefold() for item in models}
            for item in sidecars:
                if item.stem.casefold() in model_stems:
                    self._add(item, f"CAD drawing sidecar for {item.stem}")

    def assess(self, path: Path) -> ProtectionDecision:
        resolved = path.resolve()
        reasons = set(self._reasons.get(resolved, ()))
        if path.is_dir():
            for protected, items in self._reasons.items():
                if protected.is_relative_to(resolved):
                    reasons.update(items)
        evidence = tuple(sorted(reasons))
        return ProtectionDecision(bool(evidence), evidence[0] if evidence else None, evidence)


def assess_protection(
    path: Path, root: Path, *, index: ProtectionIndex | None = None,
) -> ProtectionDecision:
    """Explain whether renaming, moving, or trashing a path could break a bundle."""
    return (index or ProtectionIndex(root)).assess(path)
