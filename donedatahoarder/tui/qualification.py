"""Disposable, deterministic image fixtures for actual DDH terminal testing.

The samples are generated diagnostic drawings, not photographs or evidence of
AI quality. The default path indexes them through the real metadata pipeline.
No existing directory is ever reused, and no review action is approved/applied.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import time


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _drawing(size: tuple[int, int], label: str, *, background=(22, 32, 56)):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    width, height = size
    for x in range(0, width, max(1, width // 12)):
        draw.line((x, 0, x, height), fill=(50, 65, 95), width=2)
    for y in range(0, height, max(1, height // 8)):
        draw.line((0, y, width, y), fill=(50, 65, 95), width=2)
    corner = max(20, min(width, height) // 8)
    draw.rectangle((0, 0, corner, corner), fill=(240, 80, 90))
    draw.rectangle((width-corner, 0, width-1, corner), fill=(110, 205, 90))
    draw.rectangle((0, height-corner, corner, height-1), fill=(65, 145, 245))
    draw.rectangle((width-corner, height-corner, width-1, height-1), fill=(245, 205, 75))
    draw.ellipse((width*.2, height*.2, width*.7, height*.7), fill=(90, 160, 190))
    draw.polygon([(width*.5, height*.1), (width*.42, height*.3),
                  (width*.58, height*.3)], fill=(255, 255, 255))
    draw.text((corner + 12, 20), f"DDH TEST / {label} / TOP", fill="white", font_size=24)
    draw.text((corner + 12, height-45), "SYNTHETIC DIAGNOSTIC IMAGE", fill="white", font_size=18)
    return image


def _index_collection(root: Path, database: Path) -> dict:
    from donedatahoarder.core.jobs import job_manager
    from .service import WorkspaceService

    if job_manager.has_live_workers():
        raise RuntimeError("Finish active DDH work before preparing a separate fixture database")
    workspace = WorkspaceService(root, db_path=database)
    try:
        workspace.start_pipeline(metadata_only=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            snapshot = workspace.snapshot(limit=100)
            plan = snapshot.get("plan") or {}
            if plan.get("state") in {"completed", "failed", "cancelled", "interrupted"} and not snapshot.get("has_live_workers"):
                if plan["state"] != "completed":
                    raise RuntimeError(f"Fixture metadata pipeline {plan['state']}; inspect {database}")
                return {"session_id": workspace.session_id, "counts": snapshot["counts"],
                        "plan_state": plan["state"], "steps": plan["steps"]}
            time.sleep(.05)
        raise RuntimeError(f"Fixture preparation timed out; inspect {database} before reopening it")
    except BaseException as exc:
        # A failed snapshot can happen after a worker starts. Stop the owned
        # plan even then, and do not return while its late writes are draining.
        try:
            workspace.cancel_pipeline()
        except Exception:
            job_manager.cancel_session_jobs(workspace.session_id)
        deadline = time.monotonic() + 5
        while job_manager.has_live_workers(workspace.session_id) and time.monotonic() < deadline:
            time.sleep(.05)
        if job_manager.has_live_workers(workspace.session_id):
            raise RuntimeError(f"Fixture worker is still stopping; database kept open: {database}. Original error: {exc}") from exc
        raise
    finally:
        # The normal completed path has no live workers. On failure, cancellation
        # is cooperative; do not dispose connections still owned by a worker.
        if not job_manager.has_live_workers():
            workspace.engine.dispose()


def create_fixture(destination: Path, *, index: bool = True) -> dict:
    """Create a new self-contained fixture, manifest, and native check instructions."""
    from PIL import Image, ImageDraw
    from .diagnostics import package_versions

    destination = Path(destination).expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=False)
    root = destination / "collection"
    root.mkdir()
    records: list[dict] = []

    def save(image, relative, *, expected, **kwargs):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, **kwargs)
        records.append({"path": relative, "expected": expected,
                        "generated_sha256": _digest(path), "generated_bytes": path.stat().st_size})
        return path

    with _drawing((1200, 800), "LANDSCAPE A") as base:
        original = save(base, "00-exact/original.png", expected="Landscape with red upper-left, green upper-right, blue lower-left, yellow lower-right.")
        copy = original.with_name("copy.png")
        copy.write_bytes(original.read_bytes())
        records.append({"path": "00-exact/copy.png", "expected": "Byte-for-byte copy of original.png; compare either pair member.",
                        "generated_sha256": _digest(copy), "generated_bytes": copy.stat().st_size})
        with base.copy() as edit:
            ImageDraw.Draw(edit).rectangle((700, 420, 1000, 650), fill=(225, 95, 145))
            save(edit, "01-variants/edited.png", expected="Similar composition with a pink rectangle; distinct content, not an interchangeable copy.")
        with base.crop((200, 100, 1050, 700)) as crop:
            save(crop, "01-variants/cropped.png", expected="Different crop; missing parts of the original is intentional.")
        exif = Image.Exif()
        exif[274] = 6
        save(base, "02-orientation/exif-rotate-90.jpg", expected="EXIF 6: stored 1200x800, displayed 800x1200; corners rotate clockwise.", exif=exif)
    with _drawing((600, 1000), "PORTRAIT") as portrait:
        save(portrait, "02-orientation/portrait.png", expected="600x1000 portrait, no stretching.")
    with Image.new("RGBA", (800, 600), (0, 0, 0, 0)) as alpha:
        draw = ImageDraw.Draw(alpha)
        draw.ellipse((100, 50, 600, 550), fill=(230, 110, 150, 128))
        draw.text((140, 260), "ALPHA / TRANSPARENT BACKGROUND", fill=(255, 255, 255, 255), font_size=20)
        save(alpha, "03-formats/transparent.png", expected="Semi-transparent pink circle; background is transparent, not flattened into the theme.")
    with _drawing((400, 300), "FRAME 1") as first, _drawing((400, 300), "FRAME 2", background=(100, 30, 45)) as second:
        save(first, "03-formats/animation.gif", expected="Static first frame labeled as an animation preview, not playback.",
             save_all=True, append_images=[second], duration=400, loop=0)
    with _drawing((4000, 3000), "12 MP LARGE") as large:
        save(large, "04-large/12-megapixels.jpg", expected="4000x3000 JPEG; use uncached preview timing and zoom detail.", quality=88)
    with _drawing((800, 600), "UNICODE PATH") as unicode_image:
        save(unicode_image, "05-paths/café_旅行_תמונה.png", expected="Filename stays readable and opens the correct image.")
    with _drawing((640, 480), "WILL DISAPPEAR", background=(120, 35, 35)) as missing:
        save(missing, "06-errors/missing-after-index.png", expected="Removed after indexing; selection must explain the missing file.")
    with _drawing((640, 480), "OLD CONTENT", background=(35, 115, 45)) as changed:
        save(changed, "06-errors/changed-after-index.png", expected="Replaced after indexing with a purple NEW CONTENT image; stale review evidence must not authorize disposal.")
    corrupt = root / "06-errors/corrupt.png"
    corrupt.write_bytes(b"DDH deliberate corrupt image fixture\n")
    records.append({"path": "06-errors/corrupt.png", "expected": "Readable decode error and no old image retained.",
                    "generated_sha256": _digest(corrupt), "generated_bytes": corrupt.stat().st_size})

    manifest = {
        "schema_version": 1, "kind": "ddh-native-image-fixture", "fixture_version": 1,
        "sample_type": "synthetic diagnostic drawings; not photographs or quality benchmarks",
        "preparation": "generated", "qualification": "not_run", "packages": package_versions(),
        "root": str(root), "database": str(destination / "index.sqlite"),
        "session_id": None, "faults_applied": False, "files": records,
        "expected_exact_pairs": [["00-exact/original.png", "00-exact/copy.png"]],
        "not_interchangeable": ["01-variants/edited.png", "01-variants/cropped.png"],
        "limitations": ["Synthetic images do not qualify real-photograph performance or duplicate quality.",
                        "Near-duplicate proposals depend on the actual engine/configuration; none are seeded.",
                        "Large-source limits are covered by unit tests; the largest generated image is 12 MP."],
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if index:
        try:
            manifest["index_result"] = _index_collection(root, destination / "index.sqlite")
            manifest["session_id"] = manifest["index_result"]["session_id"]
            # These paths were created exclusively above; no caller-provided
            # manifest or user collection is ever interpreted as mutation input.
            (root / "06-errors/missing-after-index.png").unlink()
            with _drawing((640, 480), "NEW CONTENT", background=(90, 25, 135)) as replacement:
                replacement.save(root / "06-errors/changed-after-index.png")
            manifest["faults_applied"] = True
            manifest["preparation"] = "indexed"
        except Exception as exc:
            manifest["preparation"] = "failed"
            manifest["error"] = str(exc)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise
    for entry in records:
        path = root / entry["path"]
        entry["current_sha256"] = _digest(path) if path.exists() else None
    command = ["ddh", "tui", "--db", str(destination / "index.sqlite")]
    if manifest["session_id"]:
        command += ["--session", manifest["session_id"]]
    else:
        command += [str(root)]
    manifest["launch_argv"] = command
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # The saved argv is authoritative on every platform; this convenience line
    # uses POSIX quoting for the Linux target and is never executed by DDH.
    (destination / "START.txt").write_text(
        "DDH native image qualification\n\nSynthetic diagnostic samples. Nothing has been approved or applied.\n\n"
        + "Linux launch command:\n" + shlex.join(command) + "\n\n"
        + "Start in 00-exact, choose original.png, press c, and compare both images.\n"
        + "Use i for individual orientation, transparency, animation, large, Unicode, and error cases.\n"
        + "Run ddh tui-diagnostics --output NEW_REPORT.json in each terminal before launching.\n"
        + "Follow docs/tui-qualification.md and record each check; capability detection is not a pass.\n"
        + "Do not rerun metadata before testing missing/changed files: it would replace the indexed baseline.\n"
        + ("Fault injection is pending because --no-index was used.\n" if not index else "")
        + "manifest.json records expected results and file hashes. Native qualification remains not_run.\n",
        encoding="utf-8")
    return manifest
