"""
Prompt/LLM glue for rename proposals — filename translation.
"""
from pathlib import Path

_translation_cache: dict[tuple[str, str], str] = {}  # (filename, target_lang) → translated


def translate_filename(filename: str, target_language: str) -> str:
    """
    Translate a filename to the target language using LLM.

    Args:
        filename: The filename to translate (including extension)
        target_language: "english", "hebrew", or "leave_as_is"

    Returns:
        Translated filename, or original if target_language is "leave_as_is"
    """
    if target_language == "leave_as_is":
        return filename

    # Check cache first
    cache_key = (filename, target_language)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    try:
        from donedatahoarder.ai.router import get_client

        # Separate filename from extension
        stem, ext = Path(filename).stem, Path(filename).suffix

        # Build the translation prompt
        lang_name = "English" if target_language == "english" else "Hebrew"
        prompt = (
            f"Translate this filename to {lang_name}, preserving the file extension. "
            f"Return ONLY the translated filename with the extension, nothing else.\n\n"
            f"Original: {filename}"
        )

        # Call LLM for translation
        client = get_client()
        translated_filename = client.generate(prompt).strip()

        # Ensure extension is preserved
        if not translated_filename.endswith(ext):
            translated_stem = Path(translated_filename).stem
            translated_filename = translated_stem + ext

        # Cache the result
        _translation_cache[cache_key] = translated_filename
        return translated_filename

    except Exception:
        # On any error, return original filename
        return filename
