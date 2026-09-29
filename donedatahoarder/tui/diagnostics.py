"""Local, opt-in terminal qualification reports; capability is not validation."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
from typing import Any


CHECKS = (
    ("selection", "Rapid selection shows the correct image, path, and evidence."),
    ("comparison", "Both sides and every candidate render correctly in comparison."),
    ("zoom_pan", "Linked zoom/pan preserves aspect ratio and reveals original detail."),
    ("orientation", "EXIF orientation and portrait/landscape layout are correct."),
    ("transparency_animation", "Alpha displays correctly; animation is labeled first-frame only."),
    ("resize", "80x24, 120x40, wide sizes, and resize during comparison stay usable."),
    ("scroll", "Scrolling/navigation has no persistent ghost images or objectionable flicker."),
    ("theme", "Light/dark theme changes recolor the UI without tinting photos."),
    ("modal", "Dialogs cover images correctly; closing them restores the right content."),
    ("fallback", "Missing, changed, corrupt, and unsupported images have usable explanations."),
    ("external_viewer", "Open original opens the correct existing file in the desktop viewer."),
    ("exit", "Closing comparison and exiting clear images and restore normal terminal input."),
)


def package_versions() -> dict[str, str | None]:
    result = {}
    for name in ("donedatahoarder", "textual", "textual-image", "rich", "pillow", "sqlalchemy"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result


def collect_diagnostics(*, images: str = "auto", terminal_name: str | None = None,
                        terminal_version: str | None = None,
                        omarchy_version: str | None = None) -> dict[str, Any]:
    """Probe before opening the app, without opening a database or querying Ollama.

    Only an allowlist of non-secret terminal hints is collected. Hostnames,
    usernames, directories, tmux socket paths, and arbitrary environment values
    are intentionally absent. Terminal identity from the caller is labeled as
    reported; environment hints and a graphics probe cannot establish a pass.
    """
    from .images import initialize_images

    started = time.perf_counter()
    capability = initialize_images(images)
    probe_ms = round((time.perf_counter() - started) * 1000, 3)
    terminal_size = shutil.get_terminal_size(fallback=(0, 0))
    return {
        "schema_version": 1,
        "kind": "ddh-terminal-qualification",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "qualification": "not_run",
        "runtime": {"python": platform.python_version(),
                    "implementation": platform.python_implementation(),
                    "system": platform.system(), "release": platform.release(),
                    "architecture": platform.machine(), "packages": package_versions()},
        "reported_environment": {"terminal_name": terminal_name,
                                 "terminal_version": terminal_version,
                                 "omarchy_version": omarchy_version,
                                 "font_and_size": None, "display_scale": None,
                                 "reference_machine": None, "tmux_version": None},
        "terminal": {
            "hints": {key: os.environ.get(key) for key in
                      ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")},
            "tmux_present": bool(os.environ.get("TMUX")),
            "no_color": "NO_COLOR" in os.environ,
            "stdin_tty": bool(sys.stdin and sys.stdin.isatty()),
            "stdout_tty": bool(sys.stdout and sys.stdout.isatty()),
            "columns": terminal_size.columns or None, "rows": terminal_size.lines or None,
            "requested_renderer": images, "capability": asdict(capability),
            "probe_ms": probe_ms,
        },
        "checks": [{"id": key, "description": description,
                    "result": "not_run", "notes": "", "evidence": []}
                   for key, description in CHECKS],
        "measurements": {"visible_input_p95_ms": None,
                         "visible_cached_preview_p95_ms": None,
                         "visible_uncached_preview_p95_ms": None,
                         "python_peak_rss_bytes": None,
                         "sample_count": 0},
        "fixture_manifest": None,
        "notes": "Capability detection does not qualify native rendering. Complete checks in the actual terminal.",
    }


def write_report(report: dict, destination: Path) -> Path:
    """Create a report without overwriting an earlier qualification or symlink."""
    destination = Path(destination).expanduser().absolute()
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return destination
