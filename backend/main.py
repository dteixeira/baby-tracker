import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import get_db, init_db
from .scheduler import medicine_reminder_loop

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


# ── Feature flags ─────────────────────────────────────────────────────────────
# Each feature can be disabled by setting the corresponding env var to "0",
# "false", or "no" (case-insensitive). All features are enabled by default.

def _flag(name: str) -> bool:
    return os.environ.get(name, "true").strip().lower() not in {"0", "false", "no"}

FEATURES = {
    "sleep":      _flag("FEATURE_SLEEP"),
    "health":     _flag("FEATURE_HEALTH"),
    "medicines":  _flag("FEATURE_MEDICINES"),
}


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(medicine_reminder_loop()) if FEATURES["medicines"] else None
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Baby Tracker", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_sleep_type(start_time: str, end_time: str) -> str:
    """Mirror of the frontend isNight logic. Both times are ISO8601 UTC."""
    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    end   = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    start_mins = start.hour * 60 + start.minute
    duration_mins = int((end - start).total_seconds() / 60)
    if start_mins >= 20 * 60 or start_mins < 8 * 60:
        return "night"
    if start_mins >= 18 * 60 and (start_mins + duration_mins) > 20 * 60:
        return "night"
    return "nap"


# ── Sleep schemas ─────────────────────────────────────────────────────────────

class SleepCreate(BaseModel):
    start_time: str
    end_time:   Optional[str] = None
    type:       Optional[str] = None
    notes:      Optional[str] = None


class SleepStartBody(BaseModel):
    start_time: Optional[str] = None
    notes:      Optional[str] = None


class SleepUpdate(BaseModel):
    start_time: Optional[str] = None
    end_time:   Optional[str] = None
    type:       Optional[str] = None
    notes:      Optional[str] = None


# ── Sleep routes ──────────────────────────────────────────────────────────────

def _require(feature: str):
    if not FEATURES[feature]:
        raise HTTPException(404, f"Feature '{feature}' is disabled on this instance")

@app.get("/api/sleep/active")
def get_active_sleep():
    _require("sleep")
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sleep_entries WHERE end_time IS NULL ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


@app.post("/api/sleep/start", status_code=201)
def start_sleep(body: Optional[SleepStartBody] = None):
    _require("sleep")
    with get_db() as conn:
        active = conn.execute(
            "SELECT id FROM sleep_entries WHERE end_time IS NULL LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, "A sleep session is already active")
        start_time = (body.start_time if body and body.start_time else now_utc())
        cur = conn.execute(
            "INSERT INTO sleep_entries (start_time, notes) VALUES (?, ?)",
            (start_time, body.notes if body else None),
        )
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.post("/api/sleep/{sleep_id}/stop")
def stop_sleep(sleep_id: int):
    _require("sleep")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (sleep_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Sleep entry not found")
        if row["end_time"]:
            raise HTTPException(409, "Sleep session already stopped")
        end_time   = now_utc()
        sleep_type = row["type"] or classify_sleep_type(row["start_time"], end_time)
        conn.execute(
            "UPDATE sleep_entries SET end_time = ?, type = ? WHERE id = ?",
            (end_time, sleep_type, sleep_id),
        )
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (sleep_id,)).fetchone()
    return dict(row)


@app.get("/api/sleep")
def list_sleep(
    from_: Optional[str] = Query(None, alias="from"),
    to:    Optional[str] = Query(None),
    limit: int           = Query(100, ge=1, le=10000),
    offset: int          = Query(0, ge=0),
):
    _require("sleep")
    conditions, params = [], []
    if from_:
        conditions.append("start_time >= ?")
        params.append(from_)
    if to:
        conditions.append("start_time <= ?")
        params.append(to)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM sleep_entries {where} ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/sleep", status_code=201)
def create_sleep(body: SleepCreate):
    _require("sleep")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sleep_entries (start_time, end_time, type, notes) VALUES (?, ?, ?, ?)",
            (body.start_time, body.end_time, body.type, body.notes),
        )
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.patch("/api/sleep/{sleep_id}")
def update_sleep(sleep_id: int, body: SleepUpdate):
    _require("sleep")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (sleep_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Sleep entry not found")
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            return dict(row)
        # Auto-classify if end_time is now set and type is still unset
        if "end_time" in updates and not updates.get("type") and not row["type"]:
            st = updates.get("start_time", row["start_time"])
            updates["type"] = classify_sleep_type(st, updates["end_time"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE sleep_entries SET {set_clause} WHERE id = ?",
            (*updates.values(), sleep_id),
        )
        row = conn.execute("SELECT * FROM sleep_entries WHERE id = ?", (sleep_id,)).fetchone()
    return dict(row)


@app.delete("/api/sleep/{sleep_id}", status_code=204)
def delete_sleep(sleep_id: int):
    _require("sleep")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM sleep_entries WHERE id = ?", (sleep_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Sleep entry not found")
        conn.execute("DELETE FROM sleep_entries WHERE id = ?", (sleep_id,))


# ── Medicine schemas ──────────────────────────────────────────────────────────

class MedicineCreate(BaseModel):
    name:           str
    dose:           str
    interval_hours: float
    first_dose_at:  str


class MedicineUpdate(BaseModel):
    name:           Optional[str]   = None
    dose:           Optional[str]   = None
    interval_hours: Optional[float] = None
    first_dose_at:  Optional[str]   = None
    active:         Optional[int]   = None


class DoseTaken(BaseModel):
    taken_at: Optional[str] = None
    notes:    Optional[str] = None


# ── Medicine helpers ──────────────────────────────────────────────────────────

def _next_due(sched: dict, conn) -> Optional[str]:
    last = conn.execute(
        "SELECT taken_at FROM medicine_doses WHERE schedule_id = ? ORDER BY taken_at DESC LIMIT 1",
        (sched["id"],),
    ).fetchone()
    from datetime import timedelta
    anchor_str = last["taken_at"] if last else sched["first_dose_at"]
    anchor = datetime.fromisoformat(anchor_str.replace("Z", "+00:00"))
    due = anchor + timedelta(hours=sched["interval_hours"])
    return due.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Medicine routes ───────────────────────────────────────────────────────────

@app.get("/api/medicines")
def list_medicines():
    _require("medicines")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM medicine_schedules WHERE active = 1 ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            sched = dict(row)
            sched["next_due"] = _next_due(sched, conn)
            result.append(sched)
    return result


@app.get("/api/medicines/all")
def list_medicines_all():
    _require("medicines")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM medicine_schedules ORDER BY active DESC, name"
        ).fetchall()
        result = []
        for row in rows:
            sched = dict(row)
            sched["next_due"] = _next_due(sched, conn)
            result.append(sched)
    return result


@app.post("/api/medicines", status_code=201)
def create_medicine(body: MedicineCreate):
    _require("medicines")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO medicine_schedules (name, dose, interval_hours, first_dose_at) VALUES (?, ?, ?, ?)",
            (body.name, body.dose, body.interval_hours, body.first_dose_at),
        )
        row = conn.execute("SELECT * FROM medicine_schedules WHERE id = ?", (cur.lastrowid,)).fetchone()
        sched = dict(row)
        sched["next_due"] = _next_due(sched, conn)
    return sched


@app.patch("/api/medicines/{medicine_id}")
def update_medicine(medicine_id: int, body: MedicineUpdate):
    _require("medicines")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM medicine_schedules WHERE id = ?", (medicine_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Medicine schedule not found")
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            return dict(row)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE medicine_schedules SET {set_clause} WHERE id = ?",
            (*updates.values(), medicine_id),
        )
        row = conn.execute("SELECT * FROM medicine_schedules WHERE id = ?", (medicine_id,)).fetchone()
        sched = dict(row)
        sched["next_due"] = _next_due(sched, conn)
    return sched


@app.delete("/api/medicines/{medicine_id}", status_code=204)
def delete_medicine(medicine_id: int):
    _require("medicines")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM medicine_schedules WHERE id = ?", (medicine_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Medicine schedule not found")
        has_doses = conn.execute(
            "SELECT 1 FROM medicine_doses WHERE schedule_id = ? LIMIT 1", (medicine_id,)
        ).fetchone()
        if has_doses:
            raise HTTPException(409, "Cannot delete schedule with dose history; archive it instead")
        conn.execute("DELETE FROM medicine_schedules WHERE id = ?", (medicine_id,))


@app.post("/api/medicines/{medicine_id}/taken", status_code=201)
def record_dose(medicine_id: int, body: Optional[DoseTaken] = None):
    _require("medicines")
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM medicine_schedules WHERE id = ?", (medicine_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Medicine schedule not found")
        taken_at = (body.taken_at if body and body.taken_at else now_utc())
        notes    = body.notes if body else None
        cur = conn.execute(
            "INSERT INTO medicine_doses (schedule_id, taken_at, notes) VALUES (?, ?, ?)",
            (medicine_id, taken_at, notes),
        )
        dose = conn.execute("SELECT * FROM medicine_doses WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(dose)


@app.get("/api/doses")
def list_doses(
    schedule_id: Optional[int] = None,
    from_: Optional[str]       = Query(None, alias="from"),
    to:    Optional[str]       = Query(None),
):
    _require("medicines")
    conditions, params = [], []
    if schedule_id is not None:
        conditions.append("schedule_id = ?")
        params.append(schedule_id)
    if from_:
        conditions.append("taken_at >= ?")
        params.append(from_)
    if to:
        conditions.append("taken_at <= ?")
        params.append(to)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM medicine_doses {where} ORDER BY taken_at DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/doses/{dose_id}", status_code=204)
def delete_dose(dose_id: int):
    _require("medicines")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM medicine_doses WHERE id = ?", (dose_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Dose not found")
        conn.execute("DELETE FROM medicine_doses WHERE id = ?", (dose_id,))


# ── Health event schemas ──────────────────────────────────────────────────────

class HealthEventCreate(BaseModel):
    event_type: str
    value:      Optional[float] = None
    timestamp:  Optional[str]   = None
    notes:      Optional[str]   = None


class HealthEventUpdate(BaseModel):
    event_type: Optional[str]   = None
    value:      Optional[float] = None
    timestamp:  Optional[str]   = None
    notes:      Optional[str]   = None


# ── Health event routes ───────────────────────────────────────────────────────

@app.get("/api/health-events")
def list_health_events(
    event_type: Optional[str] = None,
    from_: Optional[str]      = Query(None, alias="from"),
    to:    Optional[str]      = Query(None),
):
    _require("health")
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if from_:
        conditions.append("timestamp >= ?")
        params.append(from_)
    if to:
        conditions.append("timestamp <= ?")
        params.append(to)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM health_events {where} ORDER BY timestamp DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/health-events", status_code=201)
def create_health_event(body: HealthEventCreate):
    _require("health")
    with get_db() as conn:
        ts = body.timestamp or now_utc()
        cur = conn.execute(
            "INSERT INTO health_events (event_type, value, timestamp, notes) VALUES (?, ?, ?, ?)",
            (body.event_type, body.value, ts, body.notes),
        )
        row = conn.execute("SELECT * FROM health_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.patch("/api/health-events/{event_id}")
def update_health_event(event_id: int, body: HealthEventUpdate):
    _require("health")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM health_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Health event not found")
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            return dict(row)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE health_events SET {set_clause} WHERE id = ?",
            (*updates.values(), event_id),
        )
        row = conn.execute("SELECT * FROM health_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row)


@app.delete("/api/health-events/{event_id}", status_code=204)
def delete_health_event(event_id: int):
    _require("health")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM health_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Health event not found")
        conn.execute("DELETE FROM health_events WHERE id = ?", (event_id,))


# ── Features route ────────────────────────────────────────────────────────────

@app.get("/api/features")
def get_features():
    return FEATURES


# ── Settings routes ───────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.patch("/api/settings")
def update_settings(body: dict):
    ALLOWED = {"child_name", "temp_low", "temp_high", "spo2_low", "hr_low", "hr_high"}
    invalid = set(body) - ALLOWED
    if invalid:
        raise HTTPException(400, f"Unknown settings: {', '.join(invalid)}")
    with get_db() as conn:
        for key, value in body.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Static file serving ───────────────────────────────────────────────────────
# Mounted last so /api/* routes always take priority.

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
