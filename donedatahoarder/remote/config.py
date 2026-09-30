"""Local credential files and safe listener configuration for remote sessions."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import secrets


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_token(path: Path) -> str:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Token file must be a regular file containing a private connection token.")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or len(token) > 512 or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("Connection token must contain 32–512 printable ASCII characters without whitespace.")
    return token


def ensure_token(path: Path) -> str:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_token(path)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(48) + "\n")
    return read_token(path)


def validate_listener(host: str, cert_file: Path | None, key_file: Path | None) -> None:
    if bool(cert_file) != bool(key_file):
        raise ValueError("HTTPS requires both --cert-file and --key-file.")
    if not is_loopback(host) and not (cert_file and key_file):
        raise ValueError("LAN listeners require HTTPS (--cert-file and --key-file). Alternatively, use 127.0.0.1 through an SSH tunnel.")
    for path in (cert_file, key_file):
        if path is not None and not Path(path).is_file():
            raise ValueError("The configured TLS certificate or key file does not exist.")


def validate_control_paths(paths: dict[str, Path], roots: list[Path]) -> None:
    """Keep credentials, the index, and recovery state out of scanned folders."""
    boundaries = [Path(root).expanduser().resolve() for root in roots]
    for label, path in paths.items():
        candidate = Path(path).expanduser().resolve()
        if any(candidate.is_relative_to(root) for root in boundaries):
            raise ValueError(f"Keep {label} outside authorized collection folders, on workstation internal storage.")
