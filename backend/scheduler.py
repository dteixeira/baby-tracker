import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import httpx

from .db import get_db

logger = logging.getLogger(__name__)

NTFY_URL   = os.environ.get("NTFY_URL", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "baby-tracker")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

CHECK_INTERVAL_SECONDS = 60

# Maps schedule_id → ISO timestamp of last notification sent
_last_notified: dict[int, str] = {}


async def _send_ntfy(title: str, message: str) -> None:
    if not NTFY_URL or not NTFY_TOPIC:
        return
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    payload = {
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  message,
        "priority": 4,
        "tags":     ["pill"],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(NTFY_URL.rstrip('/'), json=payload, headers=headers)
    except Exception as exc:
        logger.warning("ntfy notification failed: %s", exc)


async def _check_medicines() -> None:
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        schedules = conn.execute(
            "SELECT id, name, dose, interval_hours, first_dose_at FROM medicine_schedules WHERE active = 1"
        ).fetchall()
        child_name_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'child_name'"
        ).fetchone()
    child_name = child_name_row["value"] if child_name_row else "Baby"

    for sched in schedules:
        sched_id       = sched["id"]
        name           = sched["name"]
        dose           = sched["dose"]
        interval_hours = sched["interval_hours"]
        first_dose_at  = datetime.fromisoformat(sched["first_dose_at"].replace("Z", "+00:00"))

        with get_db() as conn:
            last_dose_row = conn.execute(
                "SELECT taken_at FROM medicine_doses WHERE schedule_id = ? ORDER BY taken_at DESC LIMIT 1",
                (sched_id,),
            ).fetchone()

        anchor = (
            datetime.fromisoformat(last_dose_row["taken_at"].replace("Z", "+00:00"))
            if last_dose_row
            else first_dose_at
        )
        next_due = anchor + timedelta(hours=interval_hours)

        if now < next_due:
            # Not yet due — clear any stale notification record
            _last_notified.pop(sched_id, None)
            continue

        # Overdue — notify at most once per overdue window
        last_notif = _last_notified.get(sched_id)
        if last_notif:
            last_notif_dt = datetime.fromisoformat(last_notif)
            if last_notif_dt >= next_due:
                continue  # Already notified for this window

        overdue_mins = int((now - next_due).total_seconds() / 60)
        title   = f"{child_name} — {name} is overdue"
        message = f"{dose} — {overdue_mins} min overdue"
        await _send_ntfy(title, message)
        _last_notified[sched_id] = now.isoformat()
        logger.info("ntfy sent for schedule %d (%s)", sched_id, name)


async def medicine_reminder_loop() -> None:
    """Background task: check medicine schedules every CHECK_INTERVAL_SECONDS."""
    logger.info("Medicine reminder loop started (interval=%ds)", CHECK_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            await _check_medicines()
        except Exception:
            logger.exception("Error in medicine reminder loop")
