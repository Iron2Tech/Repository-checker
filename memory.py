"""
memory.py
---------
Small persistent "recent files" memory, separate from the scan/report
index itself.

The index you load in opener.py (scan_XXXX.json / report_XXX.xlsx) is
still session-only by design -- you pick which one to load each run.
This module is different: it remembers what you SEARCHED FOR and OPENED
across runs, so:
  - you (or an agent) can answer "what did I have open earlier"
  - an agent can use recency as a tiebreaker when a vague request
    ("that beach photo from earlier") matches multiple files

Storage: one small JSON file. It only ever stores text metadata (name,
path, timestamp) -- never file contents -- so even after tens of
thousands of entries this stays in the KB range, nowhere near the 5GB
ceiling you mentioned. MAX_ENTRIES below is a belt-and-braces cap.

Location: ~/.iron2tech_opener/history.json by default.
For your 3-10 test users, set IRON2TECH_HISTORY_PATH to give each
person (or each demo machine) their own separate history file, e.g.
(Windows cmd): set IRON2TECH_HISTORY_PATH=C:/Users/<name>/iron2tech_history.json
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone

MAX_ENTRIES = 500  # oldest entries are dropped once history exceeds this


def _history_path():
    override = os.environ.get("IRON2TECH_HISTORY_PATH")
    if override:
        return Path(override)
    return Path.home() / ".iron2tech_opener" / "history.json"


def _load_raw():
    path = _history_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        # Corrupt/partially-written file shouldn't crash the tool.
        return []


def _save_raw(entries):
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = entries[-MAX_ENTRIES:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2)


def record_event(event_type, keyword=None, record=None):
    """event_type: 'search' or 'open'."""
    entries = _load_raw()
    entry = {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if keyword is not None:
        entry["keyword"] = keyword
    if record is not None:
        entry["name"] = record.get("name")
        entry["rel_path"] = record.get("rel_path")
        entry["abs_path"] = record.get("abs_path")
    entries.append(entry)
    _save_raw(entries)


def get_recent(limit=10, event_type=None):
    """event_type: None (both), 'search', or 'open'."""
    entries = _load_raw()
    if event_type:
        entries = [e for e in entries if e["type"] == event_type]
    return list(reversed(entries[-limit:]))


def clear_history():
    path = _history_path()
    if path.exists():
        path.unlink()
        return True
    return False
