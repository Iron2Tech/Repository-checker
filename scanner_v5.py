"""
Repository Scanner - V5
------------------------
Improvements over V4, all additive / non-breaking for report_reader.py:

1. CAPACITY / PERFORMANCE
   - Replaced `list(root.rglob("*"))` (materializes the entire tree in memory
     up front) with `os.walk`, which streams directories and lets us PRUNE
     noise folders (.git, node_modules, venv, __pycache__, etc.) before ever
     descending into them.
   - Summary mode no longer does Detailed-mode work. Previously count_lines()
     ran on every file regardless of report type; now it only runs when the
     Detailed report is requested.
   - Line counting is skipped for known-binary extensions and for any file
     over LINE_COUNT_SIZE_CAP, instead of blindly opening every file.
   - Duplicate detection uses the standard two-pass trick: bucket files by
     size first (free, we already have stat() info), then only hash files
     that share a size with at least one other file. This avoids hashing
     the entire repository just to find a handful of duplicates.
   - Large files are skipped for hashing (HASH_SIZE_CAP) so one giant file
     can't stall the whole scan.

2. REAL ANALYSIS (previously all placeholders: {})
   - duplicates   : groups of files with identical size + hash
   - empty_files  : zero-byte files
   - large_files  : top N files by size
   - hidden_files : dotfiles
   - dead_files   : heuristic "stale" files not modified in STALE_DAYS
                     (labeled clearly as a heuristic, not true dead-code
                     detection)
   - python       : file count / total lines / average lines for .py files
   - imports      : most common top-level modules imported across .py files

Schema is otherwise IDENTICAL to V4's output, so report_reader.py needs no
changes to keep working. The `analysis` section is simply no longer empty.
"""

from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
import re


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__",
    "venv", ".venv", "env",
    "dist", "build", "target",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", ".tox",
}

BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".bin", ".o", ".a",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".xz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".pyc", ".pyo", ".class", ".jar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}

LARGE_FILE_THRESHOLD_BYTES = 5 * 1024 * 1024      # flag files >= 5 MB
LINE_COUNT_SIZE_CAP_BYTES = 20 * 1024 * 1024      # don't line-count files > 20 MB
HASH_SIZE_CAP_BYTES = 50 * 1024 * 1024            # don't hash files > 50 MB
STALE_DAYS = 365                                   # "dead file" heuristic threshold
TOP_LARGE_FILES = 15
TOP_IMPORT_MODULES = 20
LIST_CAP = 200                                     # cap long lists written to JSON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_lines(file_path):
    """Safely count lines in a text file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def is_probably_text(extension):
    return extension not in BINARY_EXTENSIONS


def hash_file(file_path, block_size=65536):
    """SHA-1 hash of file contents. Returns None on failure."""
    h = hashlib.sha1()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


IMPORT_RE = re.compile(
    r"^\s*(?:import\s+([\w\.]+)|from\s+([\w\.]+)\s+import\s)", re.MULTILINE
)


def extract_python_imports(file_path):
    """Very lightweight regex-based import scan (top-level module only)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return set()

    modules = set()
    for match in IMPORT_RE.finditer(content):
        raw = match.group(1) or match.group(2)
        if raw:
            modules.add(raw.split(".")[0])
    return modules


def get_next_scan_number(scan_dir: Path):
    scan_dir.mkdir(exist_ok=True)
    existing = sorted(scan_dir.glob("scan_*.json"))

    if not existing:
        return 1

    numbers = []
    for file in existing:
        try:
            numbers.append(int(file.stem.split("_")[1]))
        except Exception:
            pass

    return max(numbers, default=0) + 1


def _capped(items, cap=LIST_CAP):
    """Return (list_capped_to_cap, total_count) for JSON-friendly output."""
    return items[:cap], len(items)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_repository(folder_path: str):

    root = Path(folder_path)

    if not root.exists():
        print("Folder does not exist.")
        return

    # -----------------------------------
    # Scan Mode
    # -----------------------------------

    print("\nSelect Scan Mode")
    print("[A] Full Sweep")
    print("[B] Extension Query")

    mode = input("Choice: ").strip().upper()

    extensions_filter = None

    if mode == "B":
        query = input("Enter extension(s) (.py,.json,.exe): ").strip().lower()
        extensions_filter = {
            ext.strip() if ext.strip().startswith(".") else "." + ext.strip()
            for ext in query.split(",")
            if ext.strip()
        }

    # -----------------------------------
    # Report Type
    # -----------------------------------

    print("\nSelect Report Type")
    print("[A] Summary")
    print("[B] Detailed")

    report_choice = input("Choice: ").strip().upper()
    detailed = report_choice == "B"

    # -----------------------------------
    # Noise exclusion
    # -----------------------------------

    print("\nSkip common noise folders? (.git, node_modules, venv, build, etc.)")
    print("[Y] Yes, skip them  [N] No, scan everything")
    exclude_choice = input("Choice: ").strip().upper()
    exclude_noise = exclude_choice != "N"

    # -----------------------------------
    # Performance timer
    # -----------------------------------

    start_time = datetime.now()
    now_ts = start_time.timestamp()

    # -----------------------------------
    # Statistics accumulators
    # -----------------------------------

    total_files = 0
    total_folders = 0
    processed_items = 0

    file_types = Counter()
    extension_index = defaultdict(list)

    empty_files = []
    hidden_files = []
    stale_files = []
    large_candidates = []          # (size, relpath)
    size_buckets = defaultdict(list)  # size -> [Path, ...]  (Detailed only)

    python_file_count = 0
    python_total_lines = 0
    import_counter = Counter()

    # -----------------------------------
    # Walk (streamed, with pruning)
    # -----------------------------------

    for dirpath, dirnames, filenames in os.walk(root):

        if exclude_noise:
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDED_DIRS]

        total_folders += len(dirnames)

        for filename in filenames:
            processed_items += 1
            item = Path(dirpath) / filename

            try:
                stat = item.stat()
            except (PermissionError, OSError):
                continue

            extension = item.suffix.lower() or "[no extension]"

            if extensions_filter is not None and extension not in extensions_filter:
                continue

            try:
                relpath = str(item.relative_to(root))
            except ValueError:
                relpath = str(item)

            total_files += 1
            file_types[extension] += 1

            hidden = filename.startswith(".")
            if hidden:
                hidden_files.append(relpath)

            if stat.st_size == 0:
                empty_files.append(relpath)

            if stat.st_size >= LARGE_FILE_THRESHOLD_BYTES:
                large_candidates.append((stat.st_size, relpath))

            age_days = (now_ts - stat.st_mtime) / 86400
            if age_days > STALE_DAYS:
                stale_files.append(relpath)

            lines = None
            if detailed:
                if is_probably_text(extension) and stat.st_size <= LINE_COUNT_SIZE_CAP_BYTES:
                    lines = count_lines(item)

                # duplicate detection: bucket by size, hash later only within buckets
                if stat.st_size > 0:
                    size_buckets[stat.st_size].append(item)

                if extension == ".py":
                    python_file_count += 1
                    python_total_lines += lines or 0
                    import_counter.update(extract_python_imports(item))

            file_info = {
                "name": item.name,
                "path": relpath,
                "extension": extension,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "lines": lines,
                "hidden": hidden,
            }

            extension_index[extension].append(file_info)

    # -----------------------------------
    # Duplicate detection (second pass, only within same-size buckets)
    # -----------------------------------

    duplicate_groups = []
    wasted_bytes = 0

    if detailed:
        for size, paths in size_buckets.items():
            if len(paths) < 2 or size > HASH_SIZE_CAP_BYTES:
                continue

            hash_map = defaultdict(list)
            for p in paths:
                h = hash_file(p)
                if h:
                    try:
                        hash_map[h].append(str(p.relative_to(root)))
                    except ValueError:
                        hash_map[h].append(str(p))

            for h, group in hash_map.items():
                if len(group) > 1:
                    duplicate_groups.append(
                        {"size_bytes": size, "count": len(group), "files": group}
                    )
                    wasted_bytes += size * (len(group) - 1)

        duplicate_groups.sort(
            key=lambda g: g["size_bytes"] * (g["count"] - 1), reverse=True
        )

    # -----------------------------------
    # Performance
    # -----------------------------------

    duration = (datetime.now() - start_time).total_seconds()

    performance = {
        "duration_seconds": duration,
        "files_per_second": round(total_files / duration, 2) if duration > 0 else 0,
        "items_processed": processed_items,
    }

    # -----------------------------------
    # Scan Metadata
    # -----------------------------------

    scan_metadata = {
        "scan_time": datetime.now().isoformat(),
        "root_folder": str(root),
        "scan_mode": "Extension Query" if extensions_filter else "Full Sweep",
        "report_type": "Detailed" if detailed else "Summary",
        "excluded_noise_folders": exclude_noise,
    }

    # -----------------------------------
    # Repository Statistics
    # -----------------------------------

    repository_statistics = {
        "folders": total_folders,
        "files": total_files,
        "file_types": dict(file_types),
    }

    # -----------------------------------
    # Analysis (real data now)
    # -----------------------------------

    if detailed:
        empty_capped, empty_total = _capped(empty_files)
        hidden_capped, hidden_total = _capped(hidden_files)
        stale_capped, stale_total = _capped(stale_files)

        large_sorted = sorted(large_candidates, key=lambda x: x[0], reverse=True)
        large_top = [
            {"path": p, "size_bytes": s} for s, p in large_sorted[:TOP_LARGE_FILES]
        ]

        analysis = {
            "duplicates": {
                "group_count": len(duplicate_groups),
                "wasted_bytes": wasted_bytes,
                "groups": duplicate_groups[:LIST_CAP],
            },
            "python": {
                "file_count": python_file_count,
                "total_lines": python_total_lines,
                "average_lines": (
                    round(python_total_lines / python_file_count, 1)
                    if python_file_count else 0
                ),
            },
            "imports": {
                "top_modules": import_counter.most_common(TOP_IMPORT_MODULES)
            },
            "dead_files": {
                "note": f"Heuristic only: files not modified in over {STALE_DAYS} days.",
                "threshold_days": STALE_DAYS,
                "count": stale_total,
                "files": stale_capped,
            },
            "empty_files": {
                "count": empty_total,
                "files": empty_capped,
            },
            "large_files": {
                "threshold_bytes": LARGE_FILE_THRESHOLD_BYTES,
                "count": len(large_candidates),
                "top": large_top,
            },
            "hidden_files": {
                "count": hidden_total,
                "files": hidden_capped,
            },
        }
    else:
        analysis = None

    # -----------------------------------
    # Build Report
    # -----------------------------------

    if not detailed:
        report = {
            "scan_metadata": scan_metadata,
            "repository_statistics": repository_statistics,
            "performance": performance,
        }
    else:
        report = {
            "scan_metadata": scan_metadata,
            "repository_statistics": repository_statistics,
            "performance": performance,
            "analysis": analysis,
            "extensions": dict(extension_index),
        }

    # -----------------------------------
    # Save
    # -----------------------------------

    scan_dir = Path("scans")
    scan_number = get_next_scan_number(scan_dir)
    output_file = scan_dir / f"scan_{scan_number:04}.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4)

    # -----------------------------------
    # Finish
    # -----------------------------------

    print("\nScan complete.")
    print(f"Report saved to:\n{output_file.resolve()}")

    if detailed:
        print(f"\nDuplicate groups found : {len(duplicate_groups)}  ({wasted_bytes:,} bytes wasted)")
        print(f"Empty files            : {len(empty_files)}")
        print(f"Hidden files            : {len(hidden_files)}")
        print(f"Stale files (>{STALE_DAYS}d)      : {len(stale_files)}")
        print(f"Large files (>= 5MB)    : {len(large_candidates)}")


if __name__ == "__main__":
    folder = input("Enter repository path: ").strip()
    scan_repository(folder)
