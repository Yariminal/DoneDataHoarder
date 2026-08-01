"""
Text utilities for folder reorganization — Hebrew transliteration, folder-name
normalisation, and mojibake detection/recovery.
"""
from __future__ import annotations

import re

# Hebrew transliteration mapping (ISO 259-3 simplified for folder naming)
_HEBREW_TRANS: dict[str, str] = {
    'א': 'a', 'ב': 'b', 'ג': 'g', 'ד': 'd', 'ה': 'h', 'ו': 'v', 'ז': 'z',
    'ח': 'ch', 'ט': 't', 'י': 'y', 'כ': 'k', 'ל': 'l', 'מ': 'm', 'נ': 'n',
    'ס': 's', 'ע': 'a', 'פ': 'p', 'צ': 'tz', 'ק': 'k', 'ר': 'r', 'ש': 'sh',
    'ת': 't', 'ך': 'k', 'ם': 'm', 'ן': 'n', 'ף': 'p', 'ץ': 'tz',
}


def _transliterate_hebrew(text: str) -> str:
    """
    Transliterate Hebrew text to Latin characters using ISO 259-3.
    Converts Hebrew to Latin, normalizes spacing.

    Examples:
      'צרפתי' → 'tzrpty' (Hebrew: French)
      'תכניות עדכניות' → 'tknyvt_adknyvt' (Hebrew: Updated Plans)
    """
    result = []
    for char in text:
        if char in _HEBREW_TRANS:
            result.append(_HEBREW_TRANS[char])
        elif char.isascii() and char.isalnum():
            result.append(char.lower())
        elif char in ('-', '_'):
            result.append('_')  # Convert hyphens to underscores, preserve underscores
        elif char.isspace():
            result.append('_')
        # else: skip non-translatable characters (punctuation, etc.)
    return ''.join(result)


def _normalize_folder_name(name: str) -> str:
    """Normalize a folder name: strip illegal chars, replace spaces with underscores."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name)
    name = name.strip("._")
    return name


# ---------------------------------------------------------------------------
# Mojibake detection — recover garbled Hebrew/Arabic/Cyrillic folder names
# ---------------------------------------------------------------------------
# Folders created on one system and copied to another with a different default
# codepage often end up with names like `êÇòÖö` — a Mac Roman reading of bytes
# that were originally cp862 (DOS Hebrew), or a cp1252 reading of cp1255 bytes.
# The LLM organizer can't see the underlying bytes, so a deterministic
# round-trip decoder is the only reliable way to recover these.

# "Basic letter" ranges — the alphabetic core of each script, excluding
# combining marks, ligatures, rare historical forms, and supplement blocks
# that usually only appear in mojibake noise. A recovery is judged meaningful
# only when most of its non-ASCII characters fall inside these ranges.
_BASIC_LETTER_RANGES = (
    (0x05D0, 0x05EA),  # Hebrew letters (aleph..tav, including finals)
    (0x0621, 0x064A),  # Arabic letters
    (0x0410, 0x044F),  # Cyrillic basic letters (А..я)
    (0x0391, 0x03C9),  # Greek letters
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
)

# Broader script ranges used only for the "already-good" guard — if a name
# contains ANY character from these, we assume the user typed it deliberately
# and leave it alone. Broader than _BASIC_LETTER_RANGES because we don't want
# to "fix" a clean name that happens to include a combining mark or ligature.
_SCRIPT_RANGES = (
    (0x0590, 0x05FF),  # Hebrew block
    (0x0600, 0x06FF),  # Arabic block
    (0x0400, 0x04FF),  # Cyrillic block
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x0370, 0x03FF),  # Greek & Coptic
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
)

# Encodings commonly used to misread cp1255 / cp862 / utf-8 Hebrew bytes.
_MOJIBAKE_WRONG_DECODINGS = ("cp1252", "latin1", "mac_roman", "utf-8")
_MOJIBAKE_RIGHT_DECODINGS = ("cp1255", "cp862", "cp1256", "cp1251", "utf-8")


def _is_basic_letter(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _BASIC_LETTER_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _is_recognised_script(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _case_alternation_ratio(text: str) -> float:
    """
    Fraction of adjacent cased-letter pairs whose case disagrees.

    Real words mostly have consistent case: 'Москва' = 1 upper + 5 lower,
    one transition out of 5 adjacent pairs = 0.2. Mojibake like 'кЗтЦц'
    alternates every character: 4 transitions out of 4 pairs = 1.0.

    Scripts without case (Hebrew, Arabic, CJK) produce 0 cased letters and
    this function returns 0.0 (no penalty), so Hebrew recoveries aren't
    punished. Only penalises cased-script recoveries.
    """
    cased = [c for c in text if c.isalpha() and (c.isupper() or c.islower())]
    if len(cased) < 2:
        return 0.0
    transitions = 0
    for a, b in zip(cased, cased[1:]):
        if a.isupper() != b.isupper():
            transitions += 1
    return transitions / (len(cased) - 1)


def _score_mojibake_recovery(text: str) -> float:
    """
    Score how "word-like" a candidate mojibake recovery is.

    Returns 0.0 when the recovery is rejected. Otherwise higher = better.

    Filters applied in order (all must pass):
    1. At least 2 basic-letter characters in a recognised script.
    2. At least 2 UNIQUE basic-letter characters — catches repeating-byte
       mojibake like 'ГЄГ‡ГІГ–Г¶' where every other char is the same 'Г'.
    3. Basic letters account for ≥50% of non-whitespace characters —
       catches single-accent false positives like 'MГјnchen' from a
       UTF-8 → cp1251 round-trip of real German text.
    4. Case-alternation ratio ≤ 0.5 for cased scripts — catches Cyrillic
       gibberish like 'кЗтЦц' (4-of-4 case transitions) while allowing
       real words ('Москва' has 1-of-5) through. Hebrew/Arabic/CJK
       recoveries aren't cased so this filter doesn't penalise them, which
       lets genuine Hebrew recoveries beat fake Cyrillic matches on ties.
    """
    if not text:
        return 0.0
    from collections import Counter

    letter_counts: Counter[str] = Counter()
    for c in text:
        if _is_basic_letter(c):
            letter_counts[c] += 1

    total_letters = sum(letter_counts.values())
    unique = len(letter_counts)
    if total_letters < 2 or unique < 2:
        return 0.0

    non_ws_total = sum(1 for c in text if not c.isspace())
    if non_ws_total == 0:
        return 0.0
    letter_ratio = total_letters / non_ws_total
    if letter_ratio < 0.5:
        return 0.0

    uniqueness = unique / total_letters
    if uniqueness < 0.5:
        return 0.0

    # Case-alternation filter — only penalises cased scripts.
    case_alt = _case_alternation_ratio(text)
    if case_alt > 0.5:
        return 0.0

    return total_letters * uniqueness


def _recover_mojibake(name: str) -> str | None:
    """
    Attempt to recover the original non-Latin folder name from mojibake.

    Returns the recovered string when a round-trip produces a result that
    passes _score_mojibake_recovery(). Returns None when the name looks fine
    as-is or no round-trip yields a meaningful recovery.

    Conservatively rejects:
    - Pure ASCII names (never mojibake)
    - Names that already contain non-Latin script characters (clean unicode)
    - Round-trips whose outputs fail the word-likeness score (real European
      names with diacritics produce low-quality "Cyrillic" noise that the
      scorer rejects)
    """
    if not name:
        return None

    # Rule 1: pure ASCII can't be mojibake.
    if all(ord(c) < 0x80 for c in name):
        return None

    # Rule 2: if the name already contains non-Latin script chars, the user
    # wrote it correctly — don't touch it. Handles clean Hebrew folder names
    # like 'example-org' which must NOT be "recovered".
    if any(_is_recognised_script(c) for c in name):
        return None

    best: str | None = None
    best_score = 0.0
    for wrong in _MOJIBAKE_WRONG_DECODINGS:
        try:
            raw = name.encode(wrong, errors="strict")
        except (UnicodeEncodeError, LookupError):
            continue
        for right in _MOJIBAKE_RIGHT_DECODINGS:
            if right == wrong:
                continue
            try:
                recovered = raw.decode(right, errors="strict")
            except (UnicodeDecodeError, LookupError):
                continue
            if recovered == name:
                continue
            # Reject results that still contain control chars or the
            # replacement codepoint — those are broken, not recovered.
            if any(ord(c) < 0x20 or c == "\ufffd" for c in recovered):
                continue
            score = _score_mojibake_recovery(recovered)
            if score > best_score:
                best = recovered
                best_score = score
    return best


def _folder_is_mojibake(name: str) -> bool:
    """True if _recover_mojibake() would yield a meaningful recovery."""
    return _recover_mojibake(name) is not None
