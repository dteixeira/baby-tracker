#!/usr/bin/env python3
"""
One-time CSV import: seeds sleep_entries from a CSV export.

Usage:
    python scripts/migrate.py --csv sleep.csv --db data/sleep.db [--timezone Europe/Lisbon]

The script is idempotent — safe to run multiple times; it deduplicates on start_time.
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ── CSV parsing (mirrors frontend logic) ──────────────────────────────────────

def parse_duration(detail: str) -> int | None:
    """Return duration in minutes, or None if unparseable."""
    s = detail.strip()
    m = re.match(r'^(\d+)\s*h\s+(\d+)\s*min$', s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r'^(\d+)\s*h$', s)
    if m:
        return int(m.group(1)) * 60
    m = re.match(r'^(\d+)\s*min$', s)
    if m:
        return int(m.group(1))
    return None


def parse_time_range(time_str: str) -> tuple[int, int] | None:
    """
    Parse 'HH:MM - HH:MM' into (start_minutes, end_minutes).
    end_minutes may be < start_minutes if the session crosses midnight.
    Returns None if unparseable.
    """
    parts = time_str.split(' - ')
    if len(parts) != 2:
        return None
    try:
        sh, sm = parts[0].strip().split(':')
        eh, em = parts[1].strip().split(':')
        return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
    except (ValueError, AttributeError):
        return None


def parse_date(date_str: str) -> tuple[int, int, int] | None:
    """Parse MM-DD-YYYY → (year, month, day), or None."""
    parts = date_str.strip().split('-')
    if len(parts) != 3:
        return None
    try:
        m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
        return y, m, d
    except ValueError:
        return None


def to_utc_iso(local_dt: datetime, tz: ZoneInfo) -> str:
    """Localise a naive datetime and convert to UTC ISO8601."""
    aware = local_dt.replace(tzinfo=tz)
    utc_dt = aware.astimezone(ZoneInfo("UTC"))
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_type(start_mins: int, duration_mins: int) -> str:
    if start_mins >= 20 * 60 or start_mins < 8 * 60:
        return "night"
    if start_mins >= 18 * 60 and (start_mins + duration_mins) > 20 * 60:
        return "night"
    return "nap"


# ── Main ──────────────────────────────────────────────────────────────────────

def migrate(csv_path: str, db_path: str, tz: ZoneInfo, dry_run: bool = False) -> None:
    # ── Read CSV
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} rows from {csv_path}")

    # ── Filter for Sleep rows
    sleep_rows = [r for r in rows if r.get('Category', '').strip() == 'Sleep']
    print(f"  → {len(sleep_rows)} rows with Category = Sleep")

    # ── Parse sessions
    sessions: list[dict] = []
    skipped = 0

    for r in sleep_rows:
        date_parsed = parse_date(r.get('Day', ''))
        if not date_parsed:
            skipped += 1
            continue

        duration_mins = parse_duration(r.get('Detail', ''))
        if not duration_mins or duration_mins <= 0:
            skipped += 1
            continue

        time_range = parse_time_range(r.get('Time', ''))
        if not time_range:
            skipped += 1
            continue

        year, month, day = date_parsed
        start_mins, end_mins = time_range

        # Build start datetime
        start_dt = datetime(year, month, day, start_mins // 60, start_mins % 60)

        # Build end datetime: add duration to start (more reliable than parsing end time)
        end_dt = start_dt + timedelta(minutes=duration_mins)

        start_utc = to_utc_iso(start_dt, tz)
        end_utc   = to_utc_iso(end_dt, tz)
        sleep_type = classify_type(start_mins, duration_mins)

        sessions.append({
            "start_time": start_utc,
            "end_time":   end_utc,
            "type":       sleep_type,
            "notes":      None,
        })

    print(f"  → {len(sessions)} valid sessions parsed, {skipped} skipped")

    if not sessions:
        print("Nothing to import.")
        return

    if dry_run:
        for s in sessions[:5]:
            print(f"  [dry-run] {s}")
        if len(sessions) > 5:
            print(f"  ... and {len(sessions) - 5} more")
        return

    # ── Init DB schema
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sleep_entries (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          start_time TEXT NOT NULL,
          end_time   TEXT,
          type       TEXT,
          notes      TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sleep_start ON sleep_entries(start_time)")

    # ── Fetch existing start_times for deduplication
    existing = {row[0] for row in conn.execute("SELECT start_time FROM sleep_entries")}
    print(f"  → {len(existing)} existing entries in DB (will skip duplicates)")

    inserted = 0
    for s in sessions:
        if s["start_time"] in existing:
            continue
        conn.execute(
            "INSERT INTO sleep_entries (start_time, end_time, type, notes) VALUES (?, ?, ?, ?)",
            (s["start_time"], s["end_time"], s["type"], s["notes"]),
        )
        existing.add(s["start_time"])
        inserted += 1

    conn.commit()
    conn.close()
    print(f"  → Inserted {inserted} new entries.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate sleep CSV into SQLite")
    parser.add_argument("--csv",      required=True, help="Path to the CSV file")
    parser.add_argument("--db",       required=True, help="Path to the SQLite database")
    parser.add_argument("--timezone", default=None,  help="Local timezone (e.g. Europe/Lisbon). Defaults to system timezone.")
    parser.add_argument("--dry-run",  action="store_true", help="Parse only, do not write to DB")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"Error: CSV file not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    tz_name = args.timezone
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(f"Error: unknown timezone '{tz_name}'", file=sys.stderr)
            sys.exit(1)
    else:
        tz = datetime.now().astimezone().tzinfo  # system local timezone
        print(f"Using system timezone: {tz}")

    migrate(args.csv, args.db, tz, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
