"""Real palette files, version compatibility, and theme replacement behavior."""
from pathlib import Path

from donedatahoarder.tui import theme


def palette_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return theme.theme_paths()


def write_palette(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_current_palette_precedes_legacy_and_supports_light_mode(tmp_path, monkeypatch):
    current, legacy = palette_paths(tmp_path, monkeypatch)
    write_palette(legacy, 'accent = "#112233"')
    write_palette(current, 'mode = "light"\nbackground = "#FFFCF0"\nforeground = "#100F0F"\naccent = "#205EA6"')
    (current.parent.parent / "theme.name").write_text("flexoki-light\n", encoding="utf-8")
    palette = theme.load_palette()
    assert palette.source == current
    assert palette.name == "flexoki-light"
    assert palette.colors["background"] == "#fffcf0"
    assert palette.colors["accent"] == "#205ea6"
    assert palette.dark is False


def test_legacy_numbered_colors_and_light_marker(tmp_path, monkeypatch):
    _, legacy = palette_paths(tmp_path, monkeypatch)
    write_palette(legacy, 'color1 = "#102030"\ncolor2 = "#203040"\ncolor4 = "#405060"\nselection_background = "#304050"')
    (legacy.parent / "light.mode").touch()
    palette = theme.load_palette()
    assert palette.source == legacy
    assert palette.colors["red"] == "#102030"
    assert palette.colors["green"] == "#203040"
    assert palette.colors["accent"] == "#405060"
    assert palette.colors["selection"] == "#304050"
    assert not palette.dark


def test_only_six_digit_rgb_colors_are_accepted(tmp_path, monkeypatch):
    current, _ = palette_paths(tmp_path, monkeypatch)
    write_palette(current, 'accent = "#ABCDEF"\nbackground = "red"\nforeground = "#123"\nred = "#123456; run anything"\ngreen = 42')
    colors = theme.read_omarchy_palette()
    assert colors["accent"] == "#abcdef"
    for key in ("background", "foreground", "red", "green"):
        assert colors[key] == theme.TOKYO_NIGHT[key]


def test_missing_malformed_and_oversized_palettes_fall_back(tmp_path, monkeypatch):
    current, _ = palette_paths(tmp_path, monkeypatch)
    assert theme.load_palette().source is None
    write_palette(current, 'background = ["#ffffff"]\naccent = "not-a-color"')
    assert theme.load_palette().source is None
    write_palette(current, 'accent = "#112233"\n' + "#" * 70_000)
    assert theme.load_palette().colors == theme.TOKYO_NIGHT


def test_fingerprint_detects_atomic_replacement_and_light_marker(tmp_path, monkeypatch):
    current, _ = palette_paths(tmp_path, monkeypatch)
    write_palette(current, 'accent = "#112233"')
    previous = theme.theme_fingerprint()
    replacement = current.with_suffix(".new")
    replacement.write_text('accent = "#445566"', encoding="utf-8")
    replacement.replace(current)
    assert theme.theme_fingerprint() != previous
    assert theme.load_palette().colors["accent"] == "#445566"
    previous = theme.theme_fingerprint()
    (current.parent / "light.mode").touch()
    assert theme.theme_fingerprint() != previous


def test_relative_xdg_paths_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    current, legacy = theme.theme_paths()
    assert current == tmp_path / ".local/state/omarchy/current/theme/colors.toml"
    assert legacy == tmp_path / ".config/omarchy/current/theme/colors.toml"
