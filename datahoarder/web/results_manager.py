"""
Results manager — save and load search result snapshots.

Allows users to manually save result sets from any tab (Files, Proposals, Duplicates)
and reload them later for comparison or review without re-running operations.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

# Results directory in user's home
RESULTS_DIR = Path.home() / ".datahoarder" / "results"


def init_results_dir() -> None:
    """Create results directory if it doesn't exist."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def save_results(result_type: str, data: Dict[str, Any], name: Optional[str] = None) -> str:
    """
    Save a result snapshot to disk.

    Args:
        result_type: "files", "proposals", or "duplicates"
        data: The result data to save (typically API response)
        name: Optional custom name; if None, uses timestamp

    Returns:
        The filename that was saved
    """
    init_results_dir()

    if not name:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{result_type}_{timestamp}"

    # Sanitize filename
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    filename = f"{name}.json"
    filepath = RESULTS_DIR / filename

    # Add metadata
    payload = {
        "type": result_type,
        "saved_at": datetime.now().isoformat(),
        "data": data,
    }

    with open(filepath, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    return filename


def load_results(filename: str) -> Optional[Dict[str, Any]]:
    """
    Load a previously saved result snapshot.

    Args:
        filename: The .json filename to load

    Returns:
        The result data, or None if file not found
    """
    filepath = RESULTS_DIR / filename

    if not filepath.exists():
        return None

    with open(filepath, "r") as f:
        payload = json.load(f)

    return payload


def list_saved_results() -> List[Dict[str, str]]:
    """
    List all saved result files.

    Returns:
        List of {filename, type, saved_at} dicts
    """
    init_results_dir()

    results = []
    for filepath in sorted(RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            with open(filepath, "r") as f:
                payload = json.load(f)
            results.append({
                "filename": filepath.name,
                "type": payload.get("type", "unknown"),
                "saved_at": payload.get("saved_at", ""),
            })
        except (json.JSONDecodeError, IOError):
            pass

    return results


def delete_results(filename: str) -> bool:
    """
    Delete a saved result file.

    Args:
        filename: The .json filename to delete

    Returns:
        True if deleted, False if not found
    """
    filepath = RESULTS_DIR / filename

    if not filepath.exists():
        return False

    filepath.unlink()
    return True
