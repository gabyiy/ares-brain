import gzip
import json
import shutil
from datetime import date
from pathlib import Path

# ===== Paths =====
BASE_DIR     = Path(__file__).resolve().parent.parent  # /home/gabi/ARES_BRAIN
LOG_DIR      = BASE_DIR / "logs" / "conversations"
ARCHIVE_DIR  = BASE_DIR / "logs" / "archive"

# Make sure archive directory exists
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _parse_log_date(path: Path):
    """
    Conversation logs are named: YYYY-MM-DD.jsonl
    Return a date object or None if the name doesn't match.
    """
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def _archive_file(log_path: Path, month_key: str):
    """
    Move one log file into the monthly archive .jsonl.gz file.
    """
    archive_name = ARCHIVE_DIR / f"{month_key}.jsonl.gz"

    # Append with gzip
    with log_path.open("rb") as src, gzip.open(archive_name, "ab") as dst:
        shutil.copyfileobj(src, dst)

    # Remove the original file
    log_path.unlink()


def main():
    today = date.today()
    archived_count = 0

    if not LOG_DIR.exists():
        print(f"No logs directory found at {LOG_DIR}")
        return

    for log_file in sorted(LOG_DIR.glob("*.jsonl")):
        log_date = _parse_log_date(log_file)
        if not log_date:
            continue

        age_days = (today - log_date).days
        if age_days <= 30:
            continue  # keep recent logs

        # Group by YYYY-MM
        month_key = log_date.strftime("%Y-%m")
        _archive_file(log_file, month_key)
        archived_count += 1

    if archived_count == 0:
        print("No old logs to archive.")
    else:
        print(f"Archived {archived_count} logs into {ARCHIVE_DIR}")

if __name__ == "__main__":
    main()
