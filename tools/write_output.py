"""
tools/write_output.py — Atomically append a validated candidate object to output/output.json.

Returns:
  {"success": True, "total_candidates": n}
  {"success": False, "error": "..."}
"""

import json
import os
import threading
from typing import Any


OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output", "output.json")
_write_lock = threading.Lock()


def write_output(candidate: dict[str, Any]) -> dict[str, Any]:
    """Append a validated candidate briefing to output/output.json atomically."""
    with _write_lock:
        return _write_locked(candidate)


def _write_locked(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        output_dir = os.path.dirname(OUTPUT_PATH)
        os.makedirs(output_dir, exist_ok=True)

        # Read existing data or initialise
        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                try:
                    existing = json.load(f)
                except json.JSONDecodeError:
                    existing = {"candidates": []}
        else:
            existing = {"candidates": []}

        if "candidates" not in existing or not isinstance(existing["candidates"], list):
            existing["candidates"] = []

        existing["candidates"].append(candidate)

        # Atomic write via temp file + rename
        tmp_path = OUTPUT_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        os.replace(tmp_path, OUTPUT_PATH)

        return {"success": True, "total_candidates": len(existing["candidates"])}

    except Exception as e:
        return {"success": False, "error": str(e)}
