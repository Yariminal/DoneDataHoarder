"""Conservative routing hints for opaque design resources.

An extension is only a policy hint. It does not establish file contents or
interchangeability, and these files remain indexed and preserved.
"""
from pathlib import Path


OPAQUE_RESOURCE_EXTENSIONS = frozenset({
    ".shx", ".ctb", ".stb", ".fon", ".ttf", ".otf",
    ".woff", ".woff2", ".eot", ".pat", ".lin",
})


def disposition(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".dxf":
        return "bounded_ascii_dxf_metadata_or_unsupported"
    if ext == ".log":
        return "cad_plot_metadata_or_existing_text_route_or_unsupported"
    if ext == ".shp":
        return "signature_checked_autocad_shape_source_metadata_or_unsupported"
    if ext in OPAQUE_RESOURCE_EXTENSIONS:
        return "preserve_opaque_font_or_cad_resource"
    if ext in {".dwg", ".3dmbak"}:
        return "preserve_opaque_design_source_or_backup"
    if ext == ".bak":
        return "backup_route_depends_on_bounded_header"
    return "analysis_route_depends_on_content_and_mime"
