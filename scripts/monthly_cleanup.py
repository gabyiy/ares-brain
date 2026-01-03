#!/usr/bin/env python3
import os
from datetime import datetime, timedelta

# --- locate project/data folders ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)              # ~/ARES_BRAIN
DATA_DIR = os.path.join(BASE_DIR, "data")
CONV_DIR = os.path.join(DATA_DIR, "conversations")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive_conversations")

os.makedirs(ARCHIVE_DIR, exist_ok=True)

def cleanup_conversations(days_keep: int = 90):
    """
    Move conversation logs older than days_keep into archive_conversations.
    """
    if not os.path.isdir(CONV_DIR):
        print("[MonthlyCleanup] No conversations directory, nothing to clean.")
        return

    cutoff = datetime.utcnow().date() - timedelta(days=days_keep)
    moved = 0

    for fname in os.listdir(CONV_DIR):
        if not fname.endswith(".jsonl"):
            continue

        # filenames are like 2025-11-25.jsonl
        try:
            date_str = fname.replace(".jsonl", "")
            fdate = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            # unexpected file name, skip
            print("[MonthlyCleanup] Skipping file with unexpected name:", fname)
            continue

        if fdate < cutoff:
            src = os.path.join(CONV_DIR, fname)
            dst = os.path.join(ARCHIVE_DIR, fname)
            os.replace(src, dst)
            moved += 1

    print(f"[MonthlyCleanup] Archived {moved} old conversation files older than {days_keep} days.")

def main():
    print("[MonthlyCleanup] Starting monthly maintenance...")
    cleanup_conversations(days_keep=90)
    print("[MonthlyCleanup] Done.")

if __name__ == "__main__":
    main()
