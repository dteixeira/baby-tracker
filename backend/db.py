import os
import sqlite3
from contextlib import contextmanager

DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/sleep.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  start_time TEXT NOT NULL,
  end_time   TEXT,
  type       TEXT,
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS medicine_schedules (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  dose           TEXT NOT NULL,
  interval_hours REAL NOT NULL,
  first_dose_at  TEXT NOT NULL,
  active         INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS medicine_doses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER NOT NULL REFERENCES medicine_schedules(id),
  taken_at    TEXT NOT NULL,
  notes       TEXT
);

CREATE TABLE IF NOT EXISTS health_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  value      REAL,
  timestamp  TEXT NOT NULL,
  notes      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sleep_start   ON sleep_entries(start_time);
CREATE INDEX IF NOT EXISTS idx_doses_schedule ON medicine_doses(schedule_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_health_ts     ON health_events(timestamp);
"""

SETTINGS_DEFAULTS = {
    "child_name": "Baby",
    "temp_low":   "36.5",
    "temp_high":  "37.5",
    "spo2_low":   "95",
    "hr_low":     "80",
    "hr_high":    "130",
}


def init_db() -> None:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executescript(SCHEMA)
        # Seed default settings if not already present
        for key, value in SETTINGS_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
