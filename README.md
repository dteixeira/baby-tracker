# baby-tracker

A homelab web app for sleep tracking, medicine scheduling, and health event logging. Runs in Docker, accessible as a PWA on phones and tablets.

## Running

```bash
docker compose up -d
```

App is available at [http://localhost:8080](http://localhost:8080).

Configure ntfy push notifications (optional) by setting environment variables before starting:

```bash
NTFY_URL=https://ntfy.your-homelab.com \
NTFY_TOPIC=baby-tracker \
NTFY_TOKEN=your-token \
docker compose up -d
```

## Importing historical sleep data (one-time)

If you have a CSV export from a sleep tracking app, you can seed the database before first launch.

**CSV format expected** (columns: `Feed #`, `Day`, `Time`, `Category`, `Type`, `Detail`, `Note`):
```
Feed #,Day,Time,Category,Type,Detail,Note
1,05-06-2026,20:01 - 22:30,Sleep,,2 h 29 min,""
```

**Run the import:**
```bash
# Install deps (first time only)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Import — replace timezone with your local timezone
.venv/bin/python3 scripts/migrate.py \
  --csv sleep.csv \
  --db data/sleep.db \
  --timezone Europe/Lisbon
```

The script is **idempotent** — safe to run multiple times, duplicates are skipped.

Use `--dry-run` to preview what would be imported without writing to the database.
