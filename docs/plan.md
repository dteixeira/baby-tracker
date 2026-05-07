# Baby Tracker — Implementation Plan

A homelab web app for sleep tracking, medicine scheduling, and health event logging. Runs in Docker, accessible as a PWA on phones and tablets.

---

## Architecture

| Layer     | Choice                                 | Reason                                                                     |
| --------- | -------------------------------------- | -------------------------------------------------------------------------- |
| Backend   | Python + FastAPI                       | Minimal setup, async, auto-docs, easy to containerize                      |
| Database  | SQLite                                 | No server process, single file, trivial backup, fine for single-family use |
| Frontend  | Vanilla JS SPA                         | No build step, the existing analysis page is already this                  |
| Container | Docker + docker-compose                | Single `docker compose up` to run on homelab                               |
| PWA       | manifest.json + minimal service worker | Just enough for "Add to Home Screen" — no offline sync needed              |

FastAPI serves both the JSON API (`/api/*`) and the static frontend files from the same process. No reverse proxy needed unless already in use.

---

## Project Structure

```
baby-tracker/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── backend/
│   ├── main.py           # FastAPI app + static file serving
│   ├── db.py             # SQLite connection, schema init, query helpers
│   └── scheduler.py      # Background task: ntfy medicine reminders
├── scripts/
│   └── migrate.py        # One-time CSV import — run locally, not exposed via API
├── frontend/
│   ├── index.html        # SPA: sleep tracker + medicine + analysis
│   ├── manifest.json     # PWA manifest
│   ├── sw.js             # Service worker (app shell cache only)
│   └── icons/            # PWA icons (192x192, 512x512, apple-touch)
└── data/                 # Volume-mounted — holds sleep.db, never committed
    └── .gitkeep
```

---

## Database Schema

```sql
-- All timestamps stored as ISO8601 UTC strings (e.g. "2026-05-07T21:30:00Z").
-- Conversion to local time happens in the frontend.

CREATE TABLE sleep_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  start_time TEXT NOT NULL,
  end_time   TEXT,            -- NULL means session is currently active
  type       TEXT,            -- 'night' | 'nap' | NULL (auto-classified on save)
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE medicine_schedules (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  dose           TEXT NOT NULL,    -- free text: "5ml", "1 tablet", "2.5mg"
  interval_hours REAL NOT NULL,    -- hours between doses, supports decimals (e.g. 4.5)
  first_dose_at  TEXT NOT NULL,    -- ISO8601: anchor time for the schedule
  active         INTEGER NOT NULL DEFAULT 1,  -- 0 = archived (soft delete)
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE medicine_doses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER NOT NULL REFERENCES medicine_schedules(id),
  taken_at    TEXT NOT NULL,   -- ISO8601
  notes       TEXT
);

CREATE TABLE health_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,   -- 'fever' | 'vomit' | 'diarrhea' | 'other'
  value      REAL,            -- temperature in °C for fever, NULL otherwise
  timestamp  TEXT NOT NULL,   -- ISO8601
  notes      TEXT
);

-- Indexes for common query patterns
CREATE INDEX idx_sleep_start ON sleep_entries(start_time);
CREATE INDEX idx_doses_schedule ON medicine_doses(schedule_id, taken_at);
CREATE INDEX idx_health_ts ON health_events(timestamp);
```

### On schedule deletion

Medicine schedules use **soft delete** (`active = 0`) rather than hard delete. This preserves dose history for reference and avoids referential integrity issues. The UI labels this action "Archive". A separate destructive "Delete everything" action can exist behind a confirmation dialog if truly needed.

---

## API

All endpoints under `/api`. Timestamps in = ISO8601 UTC. Timestamps out = ISO8601 UTC.

### Sleep

```
GET    /api/sleep              Query params: from, to (ISO dates), limit, offset
POST   /api/sleep              Body: { start_time, end_time?, type?, notes? }
PATCH  /api/sleep/:id          Body: any subset of { start_time, end_time, type, notes }
DELETE /api/sleep/:id

GET    /api/sleep/active       Returns current open session or null
POST   /api/sleep/start        Starts a new session (fails if one is already active)
POST   /api/sleep/:id/stop     Sets end_time = now, auto-classifies type if not set
```

### Medicine

```
GET    /api/medicines                  Returns active schedules with next_due computed
GET    /api/medicines/all              Includes archived
POST   /api/medicines                  Create schedule
PATCH  /api/medicines/:id              Update, or set active=0 to archive
DELETE /api/medicines/:id              Hard delete only if zero dose history

POST   /api/medicines/:id/taken        Body: { taken_at? } — defaults to now
GET    /api/doses                      Query: schedule_id, from, to
DELETE /api/doses/:id
```

### Health Events

```
GET    /api/health-events              Query: event_type, from, to
POST   /api/health-events              Body: { event_type, value?, timestamp?, notes? }
PATCH  /api/health-events/:id
DELETE /api/health-events/:id
```

---

## Frontend Views

Single HTML file, tab/section navigation in JS, no framework.

| View               | Description                                                                         |
| ------------------ | ----------------------------------------------------------------------------------- |
| **Home**           | Active sleep session (timer + stop button), next medicine due, recent health events |
| **Sleep tracker**  | Start/stop button, active timer, last 7 days list, edit/delete entries              |
| **Sleep analysis** | Existing visualization dashboard, now fetching from API                             |
| **Medicines**      | Schedule cards with next-due countdown, TAKEN button, add/archive schedules         |
| **Health log**     | Event list with add/edit/delete, simple chart                                       |

---

## Key Implementation Notes

### Timezones
Store everything in UTC. The frontend knows the local timezone (`Intl.DateTimeFormat().resolvedOptions().timeZone`) and handles all display conversions. The sleep-day (noon-to-noon) classification logic stays in JS, operating on local time derived from UTC timestamps.

### Sleep type auto-classification
The `isNight` logic from the existing frontend (hard window 20:00–08:00, gray zone 18:00–20:00 if session crosses 20:00) can be mirrored in the backend for the PATCH/stop endpoint so the `type` field is set automatically when not explicitly provided.

### Active session handling
Only one active session (no `end_time`) is allowed at a time. `POST /api/sleep/start` returns a 409 if one already exists. On container restart the session stays open in the DB — the frontend picks it up via `GET /api/sleep/active` and resumes the timer correctly.

### ntfy notifications
Configured via environment variables:
```
NTFY_URL=https://ntfy.your-homelab.com
NTFY_TOPIC=baby-tracker
NTFY_TOKEN=optional-auth-token
```
A background `asyncio` task (started in FastAPI's `lifespan`) checks every 60 seconds whether any active medicine schedule is overdue. Notifications are rate-limited (one per schedule per overdue window) by tracking last-notified time in memory.

### PWA install
`manifest.json` with `display: standalone` is enough to trigger "Add to Home Screen" on Android (Chrome) and iOS (Safari). A minimal service worker that caches the app shell (`index.html`, `manifest.json`) makes the icon load instantly. No offline data sync — the app requires local network access to the homelab.

---

## Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY frontend/ ./frontend/
EXPOSE 8080
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

```yaml
# docker-compose.yml
services:
  baby-tracker:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - DATABASE_PATH=/app/data/sleep.db
      - NTFY_URL=${NTFY_URL:-}
      - NTFY_TOPIC=${NTFY_TOPIC:-baby-tracker}
      - NTFY_TOKEN=${NTFY_TOKEN:-}
    restart: unless-stopped
```

---

## Build Order

1. **Backend skeleton** — FastAPI app, DB init, schema, static file serving, Dockerfile
2. **CSV migration** — run `scripts/migrate.py` once to seed `sleep_entries` from `sleep.csv`
3. **Sleep API + tracker UI** — start/stop button, active timer, list with edit/delete
4. **Analysis dashboard** — port existing `index.html` to fetch from `/api/sleep`
5. **Medicine tracker** — schedules CRUD, TAKEN button, next-due display, ntfy
6. **Health events** — log UI, simple visualizations
7. **PWA** — `manifest.json`, icons, service worker

---

## Migration

A standalone script (`scripts/migrate.py`) handles the one-time import of `sleep.csv` into SQLite. Run it directly against the database file before first launch — it is not exposed via the API.

```bash
python scripts/migrate.py --csv sleep.csv --db data/sleep.db
```

The script:
- Parses the CSV with the same logic currently in the frontend (MM-DD-YYYY dates, `HH:MM - HH:MM` time ranges, `Xh Ym` durations)
- Treats all times as local wall-clock time and converts to UTC using the system timezone (or a `--timezone` flag)
- Filters for `Category = Sleep` rows only
- Skips rows with unparseable durations or times
- Is idempotent — safe to run twice (deduplicates on `start_time`)

---

## Requirements

```
fastapi>=0.111
uvicorn[standard]>=0.29
httpx>=0.27        # for ntfy POST requests
```
