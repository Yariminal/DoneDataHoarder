"""Read Omarchy's active palette without importing the optional TUI stack.

Omarchy 4 keeps generated theme state under XDG_STATE_HOME; Omarchy 3 used
XDG_CONFIG_HOME. Only literal six-digit RGB colors are accepted from either.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


TOKYO_NIGHT = {
    "background": "#1a1b26",
    "foreground": "#a9b1d6",
    "bright_foreground": "#c0caf5",
    "accent": "#7aa2f7",
    "selection": "#292e42",
    "muted": "#414868",
    "red": "#f7768e",
    "green": "#9ece6a",
    "yellow": "#e0af68",
    "blue": "#7aa2f7",
    "cyan": "#449dab",
    "magenta": "#ad8ee6",
}
_ALIASES = {
    "background": ("background", "bg", "color0"),
    "foreground": ("foreground", "fg", "color7"),
    "bright_foreground": ("bright_foreground", "bright_fg", "color15"),
    "accent": ("accent", "color4", "blue"),
    "selection": ("selection", "selection_background"),
    "muted": ("muted", "color8"),
    "red": ("red", "color1"),
    "green": ("green", "color2"),
    "yellow": ("yellow", "color3"),
    "blue": ("blue", "color4"),
    "cyan": ("cyan", "color6"),
    "magenta": ("magenta", "purple", "color5"),
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_ASSIGNMENT = re.compile(r'''^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*(["'])(.*?)\2\s*(?:#.*)?$''')
_MAX_PALETTE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ThemePalette:
    colors: dict[str, str]
    name: str
    source: Path | None
    fingerprint: tuple
    dark: bool = True


def _xdg_path(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else default


def theme_paths() -> tuple[Path, Path]:
    """Current and legacy active palette paths, in precedence order."""
    home = Path.home()
    state = _xdg_path("XDG_STATE_HOME", home / ".local" / "state")
    config = _xdg_path("XDG_CONFIG_HOME", home / ".config")
    return (
        state / "omarchy" / "current" / "theme" / "colors.toml",
        config / "omarchy" / "current" / "theme" / "colors.toml",
    )


def _path_fingerprint(path: Path) -> tuple:
    try:
        stat = path.stat()
        return str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except OSError:
        return str(path), None


def theme_fingerprint() -> tuple:
    """Detect atomic theme replacement as well as in-place changes.

    Polling this small signature avoids installing or changing user theme hooks.
    """
    return tuple(
        _path_fingerprint(candidate)
        for palette in theme_paths()
        for candidate in (palette, palette.parent / "light.mode", palette.parent.parent / "theme.name")
    )


def _read_values(path: Path) -> dict[str, str]:
    # Reading a bounded amount also handles malformed or accidentally huge files.
    with path.open("rb") as stream:
        data = stream.read(_MAX_PALETTE_BYTES + 1)
    if len(data) > _MAX_PALETTE_BYTES:
        return {}
    text = data.decode("utf-8")
    try:
        import tomllib
    except ImportError:  # Keep the pure helper importable on the core CLI's 3.10.
        values = {}
        for line in text.splitlines():
            match = _ASSIGNMENT.fullmatch(line)
            if match:
                values[match[1]] = match[3]
        return values
    else:
        return tomllib.loads(text)


def _is_dark(color: str) -> bool:
    r, g, b = (int(color[offset:offset + 2], 16) / 255 for offset in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


def load_palette() -> ThemePalette:
    """Load sanitized Omarchy colors, or the bundled Tokyo Night fallback."""
    fingerprint = theme_fingerprint()
    for path in theme_paths():
        try:
            values = _read_values(path)
        except (OSError, ValueError, UnicodeError):
            continue
        colors = dict(TOKYO_NIGHT)
        accepted = 0
        for token, aliases in _ALIASES.items():
            for key in aliases:
                value = values.get(key)
                if isinstance(value, str) and _COLOR.fullmatch(value):
                    colors[token] = value.lower()
                    accepted += 1
                    break
        if not accepted:
            continue
        name = "Omarchy"
        try:
            with (path.parent.parent / "theme.name").open("r", encoding="utf-8") as stream:
                name = "".join(char for char in stream.read(128).strip() if char.isprintable()) or name
        except (OSError, UnicodeError):
            pass
        mode = values.get("mode")
        dark = mode == "dark" if mode in ("dark", "light") else _is_dark(colors["background"])
        if (path.parent / "light.mode").is_file():
            dark = False
        return ThemePalette(colors, name, path, fingerprint, dark)
    return ThemePalette(dict(TOKYO_NIGHT), "Tokyo Night", None, fingerprint)


def read_omarchy_palette() -> dict[str, str]:
    """Convenience API for callers that only need semantic color tokens."""
    return load_palette().colors
