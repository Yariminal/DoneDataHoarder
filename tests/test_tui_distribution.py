"""Artifact inspection must catch absent or stale modules before installation."""
from email.message import EmailMessage
import copy
import os
import zipfile

import pytest

from scripts.check_tui_distribution import (
    REQUIRED_MEMBERS, assert_same_distribution, inspect_wheel, isolated_environment,
)


def make_wheel(tmp_path, *, omit=None, marker='python_version >= "3.12" and extra == "tui"'):
    wheel = tmp_path / "donedatahoarder-0.6.0-py3-none-any.whl"
    metadata = EmailMessage()
    metadata["Name"] = "donedatahoarder"
    metadata["Version"] = "0.6.0"
    metadata["Requires-Python"] = ">=3.10"
    metadata["Requires-Dist"] = f"textual<9.0,>=8.2.8; {marker}"
    metadata["Requires-Dist"] = f"textual-image[textual]<0.15,>=0.14.1; {marker}"
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in REQUIRED_MEMBERS - {omit}:
            archive.writestr(member, "# packaged content\n")
        archive.writestr("donedatahoarder-0.6.0.dist-info/METADATA", metadata.as_bytes())
    return wheel


def test_missing_tui_module_is_rejected(tmp_path):
    wheel = make_wheel(tmp_path, omit="donedatahoarder/tui/images.py")
    with pytest.raises(ValueError, match="missing required.*images.py"):
        inspect_wheel(wheel)


def test_stale_source_and_new_modules_are_rejected(tmp_path):
    wheel = make_wheel(tmp_path)
    source = tmp_path / "source"
    module = source / "donedatahoarder" / "tui" / "images.py"
    module.parent.mkdir(parents=True)
    module.write_text("# changed after the build\n")
    with pytest.raises(ValueError, match="stale source.*images.py"):
        inspect_wheel(wheel, source)
    module.write_bytes(b"# packaged content\n")
    added = module.with_name("new_screen.py")
    added.write_text("# newly added module\n")
    with pytest.raises(ValueError, match="stale source.*new_screen.py"):
        inspect_wheel(wheel, source)


def test_core_python_floor_is_not_applied_to_tui_dependencies(tmp_path):
    wheel = make_wheel(tmp_path, marker='extra == "tui"')
    with pytest.raises(ValueError, match="Python 3.12 TUI marker"):
        inspect_wheel(wheel)


def test_artifact_report_records_payload_hashes(tmp_path):
    wheel = make_wheel(tmp_path)
    report = inspect_wheel(wheel)
    assert report["version"] == "0.6.0"
    assert len(report["sha256"]) == 64
    assert set(report["package_files"]) == REQUIRED_MEMBERS


def test_deleted_module_retained_by_old_build_is_rejected(tmp_path):
    wheel = make_wheel(tmp_path)
    source = tmp_path / "source"
    for member in REQUIRED_MEMBERS - {"donedatahoarder/tui/images.py"}:
        path = source / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# packaged content\n")
    with pytest.raises(ValueError, match="deleted source modules.*images.py"):
        inspect_wheel(wheel, source)


def test_installer_ignores_external_pip_install_locations(tmp_path, monkeypatch):
    redirects = ("PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_ROOT", "PIP_PYTHON")
    for name in redirects:
        monkeypatch.setenv(name, str(tmp_path / "unrelated-user-directory"))
    monkeypatch.setenv("PIP_CONFIG_FILE", str(tmp_path / "user-pip.ini"))
    env = isolated_environment(tmp_path / "disposable")
    assert all(name not in env for name in redirects)
    assert env["PIP_CONFIG_FILE"] == os.devnull


@pytest.mark.parametrize("field,new_value", [
    ("version", "0.6.1"), ("requires_dist", ["different-dependency>=1"]),
    ("entry_points", {"console_scripts": {"ddh": "other.module:main"}}),
])
def test_sdist_cannot_change_metadata_with_identical_code(tmp_path, field, new_value):
    original = inspect_wheel(make_wheel(tmp_path))
    rebuilt = copy.deepcopy(original)
    rebuilt["distribution_metadata"][field] = new_value
    with pytest.raises(ValueError, match="different version, dependency, or entrypoint"):
        assert_same_distribution(original, rebuilt)
