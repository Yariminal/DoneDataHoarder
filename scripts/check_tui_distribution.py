"""Qualify built distributions without importing the source checkout.

Run with Python 3.12+ after ``python -m build``. The smoke processes use isolated
Python, an external temporary working directory, and disposable collections.
This checks installation and headless behavior, not native terminal graphics.
"""
from __future__ import annotations

import argparse
from configparser import ConfigParser
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import venv
import zipfile


REQUIRED_MEMBERS = {
    "donedatahoarder/cli.py", "donedatahoarder/core/review.py",
    "donedatahoarder/core/photo_metadata.py", "donedatahoarder/core/photo_quality.py",
    "donedatahoarder/tui/__init__.py", "donedatahoarder/tui/app.py",
    "donedatahoarder/tui/images.py", "donedatahoarder/tui/launch.py",
    "donedatahoarder/tui/service.py", "donedatahoarder/tui/theme.py",
    "donedatahoarder/tui/discovery_screens.py", "donedatahoarder/remote/discovery.py",
    "donedatahoarder/remote/pairing.py", "donedatahoarder/remote/profiles.py",
    "donedatahoarder/remote/nearby.py", "donedatahoarder/remote/runtime.py",
    "donedatahoarder/web/templates/index.html", "donedatahoarder/web/static/app.js",
}


def inspect_wheel(path: Path, source: Path | None = None) -> dict:
    """Reject missing/stale packaged modules before testing an installed copy."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = REQUIRED_MEMBERS - names
        if missing:
            raise ValueError(f"Wheel is missing required files: {sorted(missing)}")
        if source is not None:
            package = source / "donedatahoarder"
            expected = [p for p in package.rglob("*") if p.is_file()
                        and "__pycache__" not in p.parts
                        and p.suffix in {".py", ".tcss", ".css", ".js", ".html", ".png", ".jpg",
                                         ".svg", ".json", ".txt", ".md"}]
            for item in expected:
                member = item.relative_to(source).as_posix()
                if member not in names or archive.read(member) != item.read_bytes():
                    raise ValueError(f"Wheel is missing or has stale source: {member}")
            expected_modules = {item.relative_to(source).as_posix()
                                for item in expected if item.suffix == ".py"}
            packaged_modules = {name for name in names
                                if name.startswith("donedatahoarder/") and name.endswith(".py")}
            if packaged_modules != expected_modules:
                raise ValueError(f"Wheel retains deleted source modules: {sorted(packaged_modules - expected_modules)}")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Expected exactly one wheel metadata file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if metadata["Requires-Python"] != ">=3.10":
            raise ValueError("The core Python compatibility declaration changed")
        requirements = [" ".join(value.split())
                        for value in metadata.get_all("Requires-Dist", [])]
        for dependency in ("textual", "textual-image"):
            matching = [value for value in requirements
                        if value.startswith(dependency + ("[" if dependency == "textual-image" else "<"))]
            if not any('python_version >= "3.12"' in value and 'extra == "tui"' in value
                       for value in matching):
                raise ValueError(f"Missing Python 3.12 TUI marker for {dependency}")
        payload = {name: hashlib.sha256(archive.read(name)).hexdigest()
                   for name in sorted(names) if name.startswith("donedatahoarder/")}
        entrypoints = ConfigParser()
        entrypoint_path = metadata_names[0].rsplit("/", 1)[0] + "/entry_points.txt"
        if entrypoint_path in names:
            entrypoints.read_string(archive.read(entrypoint_path).decode("utf-8"))
        distribution_metadata = {
            "name": metadata["Name"], "version": metadata["Version"],
            "requires_python": metadata["Requires-Python"], "requires_dist": sorted(requirements),
            "provides_extra": sorted(metadata.get_all("Provides-Extra", [])),
            "entry_points": {section: dict(entrypoints[section]) for section in entrypoints.sections()},
        }
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "version": metadata["Version"], "package_files": payload,
            "distribution_metadata": distribution_metadata}


def assert_same_distribution(original: dict, rebuilt: dict) -> None:
    if original["package_files"] != rebuilt["package_files"]:
        raise ValueError("Source distribution builds a different package payload than the candidate wheel")
    if original["distribution_metadata"] != rebuilt["distribution_metadata"]:
        raise ValueError("Source distribution builds different version, dependency, or entrypoint metadata")


def isolated_environment(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Python isolation does not isolate pip configuration. Inherited target or
    # prefix settings could otherwise install into a user's unrelated folder.
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PIP_TARGET",
                 "PIP_PREFIX", "PIP_USER", "PIP_ROOT", "PIP_PYTHON"):
        env.pop(name, None)
    env.update({"PYTHONUTF8": "1", "NO_COLOR": "1", "TERM": "dumb",
                "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1", "DDH_DATA_DIR": str(scratch / "journal"),
                "DDH_DB": str(scratch / "cli.db"), "XDG_DATA_HOME": str(scratch / "data")})
    return env


SMOKE = r'''
import asyncio, hashlib, importlib.metadata, json, os, pathlib, sys, time
import donedatahoarder

origin = pathlib.Path(donedatahoarder.__file__).resolve()
assert origin.is_relative_to(pathlib.Path(sys.prefix).resolve()), origin
for item in importlib.metadata.distribution("donedatahoarder").files or []:
    assert not str(item).endswith(".egg-link"), item
from PIL import Image
from donedatahoarder.ai import router
from donedatahoarder.tui.service import WorkspaceService
from donedatahoarder.tui.app import DDHApp
from donedatahoarder.tui.images import ImageCapabilities
from donedatahoarder.remote.discovery import Candidate
from donedatahoarder.remote.pairing import PairingStore, ensure_tls, parse_invitation
from donedatahoarder.remote.profiles import ProfileStore
from uuid import uuid4

def unexpected_provider(*args, **kwargs):
    raise AssertionError("Metadata-only distribution smoke called an AI provider")
router.init_ai = unexpected_provider
base = pathlib.Path(sys.argv[1]).resolve()
base.mkdir(parents=True, exist_ok=True)
server_id = str(uuid4())
certificate, key, hostname = ensure_tls(base / "tls", server_id)
pairing = PairingStore(base / "devices.sqlite3", server_id)
invitation = pairing.create_invitation(certificate.read_text(), hostname)
issued = pairing.redeem(parse_invitation(invitation)["secret"], "installed-smoke")
assert pairing.authenticate(issued["token"])
assert pairing.revoke(issued["device_id"])
assert not pairing.authenticate(issued["token"])
assert ProfileStore(base / "client").list() == []
assert Candidate(server_id, "Installed workstation", hostname, 8765, ("192.168.1.20",)).endpoint == "https://192.168.1.20:8765"
os.environ["DDH_DATA_DIR"] = str(base / "journal")
root = base / "collection"
root.mkdir(exist_ok=True)
with Image.new("RGB", (48, 32), (40, 100, 180)) as photo:
    photo.save(root / "original.png")
original = (root / "original.png").read_bytes()
(root / "copy.png").write_bytes(original)
workspace = WorkspaceService(root, db_path=base / "index.db")
try:
    workspace.start_pipeline(metadata_only=True)
    deadline = time.monotonic() + 45
    while True:
        snapshot = workspace.snapshot()
        state = (snapshot.get("plan") or {}).get("state")
        assert state not in {"failed", "cancelled", "blocked"}, snapshot.get("plan")
        if state == "completed" and not snapshot.get("has_live_workers"):
            break
        assert time.monotonic() < deadline, snapshot.get("plan")
        time.sleep(0.05)
    assert snapshot["counts"]["files"] == 2, snapshot["counts"]
    assert snapshot["counts"]["duplicates"] >= 1, snapshot["counts"]
    assert snapshot["proposals"], "Exact duplicate proposal missing"
    assert all(file["photo_metadata"]["status"] == "complete" for file in snapshot["files"])
    assert all(file["photo_metadata"]["width"] == 48 for file in snapshot["files"])
    assert snapshot["proposals"][0]["duplicate_evidence"]["photo_quality"]["status"] == "equivalent"
    assert all(path.read_bytes() == original for path in root.glob("*.png"))
    async def render():
        app = DDHApp(workspace, image_capability=ImageCapabilities(renderer="off"))
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(40):
                if app.snapshot:
                    break
                await pilot.pause(0.05)
            assert app.snapshot["counts"]["files"] == 2
            await pilot.press("2")
            await pilot.pause()
    asyncio.run(render())
    state_file = base / "installed-session.json"
    state_file.write_text(json.dumps({"session_id": workspace.session_id}), encoding="utf-8")
    print("DDH_SMOKE=" + json.dumps({
        "origin": str(origin), "session_id": workspace.session_id,
        "files": snapshot["counts"]["files"], "duplicates": snapshot["counts"]["duplicates"],
        "metadata_pipeline": state, "headless_tui": "passed",
        "nearby_pairing_and_revocation": "passed",
        "versions": {name: importlib.metadata.version(name)
                     for name in ("donedatahoarder", "textual", "textual-image", "Pillow", "zeroconf", "cryptography")},
    }))
finally:
    workspace.engine.dispose()
'''

RESUME = r'''
import json, os, pathlib, sys
from donedatahoarder.tui.service import WorkspaceService
base = pathlib.Path(sys.argv[1]).resolve()
os.environ["DDH_DATA_DIR"] = str(base / "journal")
saved = json.loads((base / "installed-session.json").read_text(encoding="utf-8"))
service = WorkspaceService(session_id=saved["session_id"], db_path=base / "index.db")
try:
    snapshot = service.snapshot()
    assert snapshot["counts"]["files"] == 2, snapshot["counts"]
    assert snapshot["plan"]["state"] == "completed", snapshot["plan"]
    print("Installed session resumed")
finally:
    service.engine.dispose()
'''


def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 240) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command!r}\n"
                           f"{result.stdout}\n{result.stderr}")
    return result.stdout


def python_in(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def entrypoint_in(environment: Path) -> Path:
    return environment / ("Scripts/ddh.exe" if os.name == "nt" else "bin/ddh")


def smoke(python: Path, command: Path, scratch: Path, env: dict[str, str], state: Path) -> dict:
    for args in ([], ["tui"], ["tui-fixture"], ["tui-diagnostics"]):
        output = run([str(command), *args, "--help"], scratch, env)
        if "Usage" not in output:
            raise RuntimeError(f"CLI help did not render: {args}")
    output = run([str(python), "-I", "-c", SMOKE, str(state)], scratch, env, timeout=90)
    line = next((line for line in output.splitlines() if line.startswith("DDH_SMOKE=")), None)
    if line is None:
        raise RuntimeError(f"Installed smoke did not report success: {output}")
    run([str(python), "-I", "-c", RESUME, str(state)], scratch, env)
    run([str(command), "tui-fixture", str(state / "qualification"), "--no-index"], scratch, env)
    if not (state / "qualification" / "manifest.json").is_file():
        raise RuntimeError("The installed fixture command did not create its manifest")
    diagnostics = state / "terminal-report.json"
    run([str(command), "tui-diagnostics", "--images", "off", "--output", str(diagnostics)], scratch, env)
    captured = json.loads(diagnostics.read_text(encoding="utf-8"))
    if captured.get("qualification") != "not_run":
        raise RuntimeError("Headless diagnostics must not claim native terminal qualification")
    result = json.loads(line.split("=", 1)[1])
    result["fixture_and_diagnostics"] = "passed (no-index fixture; images off)"
    return result


def qualify(wheel: Path, sdist: Path, source: Path, uv: Path | None = None) -> dict:
    if sys.version_info < (3, 12):
        raise RuntimeError("The installed TUI smoke requires Python 3.12 or newer")
    original = inspect_wheel(wheel, source)
    report = {"python": sys.version, "platform": platform.platform(), "wheel": original,
              "sdist": {"name": sdist.name, "sha256": hashlib.sha256(sdist.read_bytes()).hexdigest()},
              "native_terminal_graphics": "not tested (headless)"}
    with tempfile.TemporaryDirectory(prefix="ddh-distribution-") as tmp:
        scratch = Path(tmp).resolve()
        if scratch.is_relative_to(source):
            raise RuntimeError("Distribution verification must run outside the source checkout")
        env = isolated_environment(scratch)
        environment = scratch / "installed"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = python_in(environment)
        pip = [str(python), "-I", "-m", "pip"]
        print("Installing built wheel in an isolated environment...", flush=True)
        run([*pip, "install", f"{wheel}[tui,docs,remote,nearby]"], scratch, env)
        run([*pip, "check"], scratch, env)
        report["wheel_smoke"] = smoke(python, entrypoint_in(environment), scratch, env,
                                       scratch / "wheel-state")
        report["resolved_dependencies"] = json.loads(run([*pip, "list", "--format=json"], scratch, env))
        report["qualification_constraints"] = "\n".join(
            f"{item['name']}=={item['version']}"
            for item in sorted(report["resolved_dependencies"], key=lambda item: item["name"].lower())
            if item["name"].lower() not in {"donedatahoarder", "pip", "setuptools", "wheel"}
        ) + "\n"
        constraints = scratch / "qualification.constraints.txt"
        constraints.write_text(report["qualification_constraints"], encoding="utf-8")
        print("Rebuilding and installing the source distribution...", flush=True)
        rebuilt_dir = scratch / "rebuilt"
        run([*pip, "wheel", "--no-deps", "--wheel-dir", str(rebuilt_dir), str(sdist)], scratch, env)
        rebuilt = next(rebuilt_dir.glob("donedatahoarder-*.whl"))
        rebuilt_info = inspect_wheel(rebuilt)
        assert_same_distribution(original, rebuilt_info)
        run([*pip, "install", "--force-reinstall", "--no-deps", str(rebuilt)], scratch, env)
        run([str(python), "-I", "-c", RESUME, str(scratch / "wheel-state")], scratch, env)
        report["sdist_smoke"] = smoke(python, entrypoint_in(environment), scratch, env,
                                       scratch / "sdist-state")
        report["reinstall_resume"] = "passed (same candidate; not a cross-version migration test)"
        if uv is not None:
            print("Checking isolated uv tool installation and removal...", flush=True)
            env.update({"UV_TOOL_DIR": str(scratch / "uv-tools"),
                        "UV_TOOL_BIN_DIR": str(scratch / "uv-bin"),
                        "UV_CACHE_DIR": str(scratch / "uv-cache"),
                        "UV_PYTHON_DOWNLOADS": "never"})
            install = [str(uv), "tool", "install", "--python", sys.executable,
                       "--constraints", str(constraints),
                       f"{wheel}[tui,docs,remote,nearby]"]
            run(install, scratch, env)
            uv_environment = scratch / "uv-tools" / "donedatahoarder"
            uv_command = scratch / "uv-bin" / ("ddh.exe" if os.name == "nt" else "ddh")
            report["uv_smoke"] = smoke(python_in(uv_environment), uv_command, scratch, env,
                                        scratch / "uv-state")
            run([*install[:-1], "--force", install[-1]], scratch, env)
            run([str(python_in(uv_environment)), "-I", "-c", RESUME,
                 str(scratch / "uv-state")], scratch, env)
            run([str(uv), "tool", "uninstall", "donedatahoarder"], scratch, env)
            if uv_command.exists() or not (scratch / "uv-state" / "index.db").is_file():
                raise RuntimeError("uv uninstall removed application data or retained the entrypoint")
            report["uv_version"] = run([str(uv), "--version"], scratch, env).strip()
            report["uv_install_arguments"] = install[1:]
            report["uv_reinstall_resume_and_uninstall"] = "passed; saved index retained"
        else:
            report["uv_smoke"] = "not requested"
    # A shared checkout may change while dependencies are being installed. Do
    # not present a successful old candidate as qualification of those edits.
    inspect_wheel(wheel, source)
    report["checked_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--uv", type=Path, help="Optional uv executable; uses temporary tool/cache directories")
    parser.add_argument("--report", type=Path, default=Path("dist/tui-distribution-report.json"))
    args = parser.parse_args()
    wheels = sorted(args.dist.glob("donedatahoarder-*.whl"))
    sdists = sorted(args.dist.glob("donedatahoarder-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        parser.error("Choose a --dist folder containing exactly one wheel and one sdist")
    started = time.monotonic()
    report = qualify(wheels[0].resolve(), sdists[0].resolve(), args.source.resolve(),
                     args.uv.resolve() if args.uv else None)
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.report.with_suffix(".constraints.txt").write_text(
        "# Versions exercised by this report on " + report["platform"] + "\n"
        "# Keep with the report and wheel; this is not a cross-platform release lock.\n"
        + report["qualification_constraints"], encoding="utf-8")
    print(f"Distribution checks passed. Report: {args.report.resolve()}")


if __name__ == "__main__":
    main()
