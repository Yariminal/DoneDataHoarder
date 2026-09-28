"""Conservative guards for files whose relative references would break on move.

This is an execution-time index, not a file rewriter. Unresolved references
keep their source protected; known referenced resources are protected too.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


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
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)|@import\s+['\"]([^'\"]+)['\"]", re.I)
_LINKED_HINT = re.compile(
    rb"(?:[A-Za-z0-9_. -]+[/\\])*[A-Za-z0-9_. -]+\.(?:png|jpe?g|tiff?|psd|svg|pdf|eps|ai)", re.I,
)
_REFERENCE_READ_LIMIT = 2_000_000


class _LocalHTMLReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.base_href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "base":
            self.base_href = next((value for key, value in attrs if key == "href"), None)
        for key, value in attrs:
            if not value:
                continue
            if key in {"src", "href", "poster", "data", "background"}:
                self.references.append(value)
            elif key == "srcset":
                self.references.extend(part.strip().split()[0] for part in value.split(",")
                                       if part.strip())
            elif key == "style":
                self.references.extend(_css_references(value))


def _css_references(text: str) -> list[str]:
    return [match.group(2) or match.group(3) for match in _CSS_URL.finditer(text)]


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
        self._opaque_scopes: set[tuple[Path, str]] = set()
        self._build()

    def _add(self, path: Path, reason: str) -> None:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return
        if resolved.is_relative_to(self.root):
            self._reasons.setdefault(resolved, set()).add(reason)

    @staticmethod
    def _is_link(path: Path) -> bool:
        """Windows junctions are reparse points but are not symlinks."""
        try:
            if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                return True
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except FileNotFoundError:
            return False
        except OSError:
            return True

    def _safe_reference_path(self, source: Path, raw: str) -> Path | None:
        """Resolve an in-root relative reference without probing outside it."""
        name = unquote(raw.strip().strip('"')).replace("\\", "/")
        if (not name or re.match(r"^[A-Za-z]:", name) or name.startswith("/")
                or name.startswith("//")):
            return None
        # Normalize parent traversal lexically before touching the filesystem.
        lexical = Path(os.path.normpath(str(source.parent / name)))
        if not lexical.is_relative_to(self.root):
            return None
        # A symlink can point outside the collection, even when its spelling
        # looks local. Reject it without following its target.
        current = lexical
        while current != self.root:
            if self._is_link(current):
                return None
            current = current.parent
        try:
            resolved = lexical.resolve()
        except (OSError, RuntimeError):
            return None
        return resolved if resolved.is_relative_to(self.root) else None

    def _web_reference(self, source: Path, raw: str, kind: str) -> None:
        """Ignore remote/data anchors; retain paths for local or ambiguous links."""
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            return
        try:
            parsed = urlsplit(raw)
        except ValueError:
            self._add(source, f"Malformed {kind} reference: {raw[:120]}")
            self._protect_opaque_subtree(source, "web")
            return
        if parsed.scheme in {"http", "https", "mailto", "tel", "data", "javascript"}:
            return
        if parsed.scheme or parsed.netloc:
            self._add(source, f"Unresolved {kind} reference: {raw[:120]}")
            return
        if parsed.path:
            if parsed.path.startswith("/"):
                # A site-root URL may refer beneath the collection root, but
                # the actual serving root is unknown. Keep any local match.
                target = self._safe_reference_path(
                    self.root / "_site_root_marker", parsed.path.lstrip("/"))
                self._add(source, f"Site-root {kind} reference: {raw[:120]}")
                if target is not None and target.is_file():
                    self._add(target, f"Referenced by {source.name}: {raw[:120]}")
                # The serving root is unknown even when the collection-root
                # spelling exists: /assets/foo may resolve beneath a nested
                # site instead. Do not treat one path as exhaustive evidence.
                self._protect_opaque_subtree(source, "web", scope=self.root)
            else:
                self._referenced(source, parsed.path, kind)

    def _protect_opaque_subtree(
        self, source: Path, kind: str, *, scope: Path | None = None,
    ) -> None:
        """Keep likely local assets in the smallest known source subtree.

        A root-level page has no narrower verified site root, so guard known
        web/design asset formats under the collection rather than every file.
        """
        search_root = scope or source.parent
        scope_key = (search_root, kind)
        if scope_key in self._opaque_scopes:
            return
        self._opaque_scopes.add(scope_key)
        suffixes = (_TEXTURES | {".svg", ".css", ".js", ".mjs", ".json", ".html",
                                 ".htm", ".woff", ".woff2", ".ttf", ".otf", ".ico"}
                    if kind == "web" else
                    _TEXTURES | {".psd", ".ai", ".eps", ".svg", ".pdf"})
        for candidate in self._iter_tree(search_root):
            if candidate.is_file() and candidate.suffix.lower() in suffixes:
                self._add(candidate, f"Potential unresolved {kind} dependency of {source.name}")

    def _inspect_web_file(self, path: Path, ext: str) -> None:
        try:
            with path.open("rb") as stream:
                data = stream.read(_REFERENCE_READ_LIMIT + 1)
        except OSError:
            self._add(path, "Web dependency references could not be inspected")
            self._protect_opaque_subtree(path, "web")
            return
        if len(data) > _REFERENCE_READ_LIMIT:
            self._add(path, "Web references beyond inspected prefix are unknown")
            self._protect_opaque_subtree(path, "web")
        content = data[:_REFERENCE_READ_LIMIT].decode("utf-8", errors="replace")
        refs: list[str] = []
        if ext in {".html", ".htm"}:
            parser = _LocalHTMLReferences()
            try:
                parser.feed(content)
                refs.extend(parser.references)
                if parser.base_href:
                    base = urlsplit(parser.base_href)
                    if base.scheme or base.netloc or base.path.startswith("/"):
                        self._add(path, "HTML base URL changes reference resolution")
                        self._protect_opaque_subtree(path, "web", scope=self.root)
                    elif base.path:
                        refs = [str(Path(base.path) / ref) if not urlsplit(ref).scheme
                                and not ref.startswith(("/", "#")) else ref for ref in refs]
            except Exception:
                self._add(path, "HTML references could not be fully parsed")
                self._protect_opaque_subtree(path, "web")
        refs.extend(_css_references(content))
        for raw in refs:
            self._web_reference(path, raw, "web asset")

    def _inspect_design_links(self, path: Path) -> None:
        """Recover inspectable local path hints; opaque formats stay protected."""
        self._add(path, "Design source may contain opaque linked assets")
        try:
            with path.open("rb") as stream:
                data = stream.read(_REFERENCE_READ_LIMIT + 1)
        except OSError:
            self._add(path, "Design links could not be inspected")
            self._protect_opaque_subtree(path, "design")
            return
        if len(data) > _REFERENCE_READ_LIMIT:
            self._add(path, "Design links beyond inspected prefix are unknown")
        for raw in set(_LINKED_HINT.findall(data[:_REFERENCE_READ_LIMIT])):
            hint = raw.decode("utf-8", errors="replace").strip()
            if hint and hint != path.name:
                self._referenced(path, hint, "design asset")
        # Binary PSD/AI link records can be encoded or compressed. Nearby
        # resources remain in place when no complete link inventory exists.
        self._protect_opaque_subtree(path, "design")

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
            suffixes = ({".mtl"} if kind == "OBJ material" else
                        _TEXTURES | {".css", ".js", ".svg", ".html", ".htm", ".woff", ".woff2"}
                        if kind == "web asset" else _TEXTURES)
            self._protect_potential_resources(source, suffixes, kind)
            if kind == "web asset":
                self._protect_opaque_subtree(source, "web")
            elif kind == "design asset":
                self._protect_opaque_subtree(source, "design")

    def _iter_tree(self, start: Path | None = None):
        """Walk without entering Windows junctions or symlinked directories."""
        for directory, names, files in os.walk(start or self.root, followlinks=False):
            parent = Path(directory)
            safe_names = []
            for name in names:
                child = parent / name
                if self._is_link(child):
                    self._add(parent, f"Contains linked directory: {name}")
                else:
                    safe_names.append(name)
                    yield child
            names[:] = safe_names
            for name in files:
                child = parent / name
                if self._is_link(child):
                    self._add(parent, f"Contains linked file: {name}")
                else:
                    yield child

    def _protect_potential_resources(self, source: Path, suffixes: set[str], kind: str) -> None:
        try:
            siblings = source.parent.iterdir()
            for sibling in siblings:
                if not self._is_link(sibling) and sibling.is_file() and sibling.suffix.lower() in suffixes:
                    self._add(sibling, f"Potential unresolved {kind} dependency of {source.name}")
        except OSError:
            self._add(source, f"Potential {kind} dependencies could not be inspected")

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
            for candidate in self._iter_tree(report.parent):
                if candidate.is_file() and candidate.suffix.lower() in _ETRANSMIT_RESOURCES:
                    self._add(candidate, f"Possible unresolved eTransmit dependency of {report.name}")

    def _build(self) -> None:
        # Stream the tree; retain only directories with recognized CAD
        # members, rather than buffering every path in a large collection.
        cad_by_dir: dict[Path, tuple[list[Path], list[Path], list[Path]]] = {}
        for path in self._iter_tree():
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
            if ext in {".html", ".htm", ".css"}:
                self._inspect_web_file(path, ext)
            if ext in {".ai", ".psd"}:
                self._inspect_design_links(path)
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
        if self._is_link(path):
            reason = "Linked path cannot be safely relocated"
            return ProtectionDecision(True, reason, (reason,))
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            reason = "Path could not be resolved for dependency protection"
            return ProtectionDecision(True, reason, (reason,))
        if not resolved.is_relative_to(self.root):
            reason = "Path resolves outside the collection root"
            return ProtectionDecision(True, reason, (reason,))
        reasons = set(self._reasons.get(resolved, ()))
        if path.is_dir():
            for protected, items in self._reasons.items():
                if protected.is_relative_to(resolved):
                    reasons.update(items)
        evidence = tuple(sorted(reasons))
        return ProtectionDecision(bool(evidence), evidence[0] if evidence else None, evidence)


_READ_CACHE_LOCK = threading.Lock()
_READ_CACHE: OrderedDict[Path, tuple[float, int, ProtectionIndex]] = OrderedDict()
_READ_CACHE_MAX_ROOTS = 8


def cached_protection_index(root: Path, ttl_seconds: float = 5.0) -> ProtectionIndex:
    """Short-lived snapshot for read-only review pages.

    Review data can be a few seconds old; preview and commit must construct a
    fresh ProtectionIndex and independently enforce the current filesystem.
    """
    resolved = root.resolve()
    stamp = resolved.stat().st_mtime_ns
    now = time.monotonic()
    with _READ_CACHE_LOCK:
        cached = _READ_CACHE.get(resolved)
        if cached and now - cached[0] < ttl_seconds and cached[1] == stamp:
            _READ_CACHE.move_to_end(resolved)
            return cached[2]
    index = ProtectionIndex(resolved)
    with _READ_CACHE_LOCK:
        _READ_CACHE[resolved] = (time.monotonic(), stamp, index)
        _READ_CACHE.move_to_end(resolved)
        while len(_READ_CACHE) > _READ_CACHE_MAX_ROOTS:
            _READ_CACHE.popitem(last=False)
    return index


def assess_protection(
    path: Path, root: Path, *, index: ProtectionIndex | None = None,
) -> ProtectionDecision:
    """Explain whether renaming, moving, or trashing a path could break a bundle."""
    return (index or ProtectionIndex(root)).assess(path)
