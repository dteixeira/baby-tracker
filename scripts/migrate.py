#!/usr/bin/env python3
"""
One-time CSV import: seeds sleep_entries from a CSV export.

Usage (direct DB):
    python scripts/migrate.py --csv sleep.csv --db data/sleep.db [--timezone Europe/Lisbon]

Usage (remote API):
    python scripts/migrate.py --csv sleep.csv --server http://localhost:8080 [--timezone Europe/Lisbon]

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


# ── Session parsing ───────────────────────────────────────────────────────────

def parse_sessions(csv_path: str, tz: ZoneInfo) -> list[dict]:
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} rows from {csv_path}")

    sleep_rows = [r for r in rows if r.get('Category', '').strip() == 'Sleep']
    print(f"  → {len(sleep_rows)} rows with Category = Sleep")

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

        start_dt = datetime(year, month, day, start_mins // 60, start_mins % 60)
        end_dt   = start_dt + timedelta(minutes=duration_mins)

        sessions.append({
            "start_time": to_utc_iso(start_dt, tz),
            "end_time":   to_utc_iso(end_dt, tz),
            "type":       classify_type(start_mins, duration_mins),
            "notes":      None,
        })

    print(f"  → {len(sessions)} valid sessions parsed, {skipped} skipped")
    return sessions


# ── Import: direct DB ─────────────────────────────────────────────────────────

def migrate_db(sessions: list[dict], db_path: str) -> None:
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


# ── Import: remote API ────────────────────────────────────────────────────────

def migrate_api(sessions: list[dict], server: str) -> None:
    try:
        import httpx
    except ImportError:
        print("Error: httpx is required for --server mode. Run: pip install httpx", file=sys.stderr)
        sys.exit(1)

    base = server.rstrip('/')

    # Fetch all existing start_times for deduplication (paginated)
    print(f"  → Fetching existing entries from {base}/api/sleep …")
    existing: set[str] = set()
    offset = 0
    limit  = 1000
    with httpx.Client(base_url=base, timeout=30) as client:
        while True:
            resp = client.get("/api/sleep", params={"limit": limit, "offset": offset})
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            for entry in page:
                existing.add(entry["start_time"])
            if len(page) < limit:
                break
            offset += limit

        print(f"  → {len(existing)} existing entries on server (will skip duplicates)")

        to_insert = [s for s in sessions if s["start_time"] not in existing]
        print(f"  → Inserting {len(to_insert)} new entries…")

        inserted = 0
        errors   = 0
        for s in to_insert:
            try:
                resp = client.post("/api/sleep", json=s)
                resp.raise_for_status()
                inserted += 1
            except httpx.HTTPStatusError as e:
                print(f"  [warn] {s['start_time']} → HTTP {e.response.status_code}", file=sys.stderr)
                errors += 1
            except httpx.RequestError as e:
                print(f"  [error] {s['start_time']} → {e}", file=sys.stderr)
                errors += 1

    print(f"  → Inserted {inserted} new entries ({errors} errors).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate sleep CSV into Baby Tracker")
    parser.add_argument("--csv",      required=True,      help="Path to the CSV file")
    parser.add_argument("--timezone", default=None,        help="Local timezone (e.g. Europe/Lisbon). Defaults to system timezone.")
    parser.add_argument("--dry-run",  action="store_true", help="Parse only, do not write anything")

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--db",     help="Path to the SQLite database (direct import)")
    target.add_argument("--server", help="Base URL of a running Baby Tracker instance (e.g. http://localhost:8080)")

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
        tz = datetime.now().astimezone().tzinfo
        print(f"Using system timezone: {tz}")

    sessions = parse_sessions(args.csv, tz)

    if not sessions:
        print("Nothing to import.")
        return

    if args.dry_run:
        for s in sessions[:5]:
            print(f"  [dry-run] {s}")
        if len(sessions) > 5:
            print(f"  ... and {len(sessions) - 5} more")
        return

    if args.db:
        migrate_db(sessions, args.db)
    else:
        migrate_api(sessions, args.server)


if __name__ == "__main__":
    main()
