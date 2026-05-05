"""
main.py — FastAPI application.

Endpoints:
  POST /telemetry            — Ingest a sensor reading, trigger alert logic
  GET  /devices/{id}/status  — Last 50 readings + current alert state
  GET  /health               — Health check

Startup:
  - Creates DB tables
  - Initializes Twilio client
  - Spawns the background silent-failure checker
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()  # Load .env before anything reads os.getenv

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from database import engine, get_db, Base
from models import (
    TelemetryReading, DeviceState,
    TelemetryIn, TelemetryResponse,
    DeviceStatusResponse, AlertInfo, ReadingOut,
)
from alert_engine import check_alerts, init_twilio, silent_failure_checker

# ── Logging ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-30s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("factory_sense.main")


# ── Lifespan (startup + shutdown) ───────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Replaces the deprecated @app.on_event('startup')."""

    # ── Startup ─────────────────────────────────────────────────
    logger.info("🏭 FactorySense — Starting up...")
    Base.metadata.create_all(bind=engine)
    logger.info("📦 Database tables created / verified")

    init_twilio()

    worker = asyncio.create_task(silent_failure_checker())
    logger.info("🚀 FactorySense — Ready!")

    yield

    # ── Shutdown ────────────────────────────────────────────────
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    logger.info("🛑 FactorySense — Shut down.")


# ── App ─────────────────────────────────────────────────────────

app = FastAPI(
    title="FactorySense — Sensor Telemetry & Alert Pipeline",
    description=(
        "Ingests ESP32 sensor data, detects temperature/vibration breaches "
        "and device silence, manages a deduplication state machine, "
        "and sends WhatsApp alerts via Twilio."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── POST /telemetry ─────────────────────────────────────────────

@app.post("/telemetry", response_model=TelemetryResponse, tags=["Telemetry"])
def ingest_telemetry(payload: TelemetryIn, db: Session = Depends(get_db)):
    """
    Ingest a telemetry reading from an ESP32 device.

    On every call:
      1. Persist the raw reading to `telemetry_readings`
      2. Update consecutive breach counters in `device_state`
      3. Evaluate the alert state machine
      4. Send WhatsApp ONLY on state transitions (dedup)
    """
    # Parse the timestamp string
    try:
        ts = datetime.fromisoformat(payload.timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp format. Use ISO 8601.")

    # Persist reading
    reading = TelemetryReading(
        device_id=payload.device_id,
        timestamp=ts,
        temperature_c=payload.temperature_c,
        vibration_g=payload.vibration_g,
    )
    db.add(reading)
    db.flush()

    # Run alert engine
    result = check_alerts(
        db,
        payload.device_id,
        temperature_c=payload.temperature_c,
        vibration_g=payload.vibration_g,
    )

    logger.info(
        "📥 %s │ temp=%6.1f°C │ vibe=%5.2fg │ → %s",
        payload.device_id, payload.temperature_c,
        payload.vibration_g, result["transition"],
    )

    return TelemetryResponse(
        status="ok",
        device_id=payload.device_id,
        alert_transition=result["transition"],
        alert_type=result.get("alert_type"),
        message=result.get("message", ""),
    )


# ── GET /devices/{device_id}/status ─────────────────────────────

@app.get("/devices/{device_id}/status", response_model=DeviceStatusResponse, tags=["Devices"])
def get_device_status(device_id: str, db: Session = Depends(get_db)):
    """Return the last 50 readings and current alert state for a device."""

    state = db.query(DeviceState).filter(DeviceState.device_id == device_id).first()
    if state is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")

    readings = (
        db.query(TelemetryReading)
        .filter(TelemetryReading.device_id == device_id)
        .order_by(TelemetryReading.timestamp.desc())
        .limit(50)
        .all()
    )

    alert = AlertInfo(
        alert_state=state.alert_state,
        alert_state_label="ALERT" if state.alert_state == 1 else "NORMAL",
        alert_type=state.alert_type,
        last_seen=state.last_seen.isoformat() if state.last_seen else None,
        consecutive_temp_breaches=state.consecutive_temp_breaches,
        consecutive_vibe_breaches=state.consecutive_vibe_breaches,
    )

    recent = [
        ReadingOut(
            id=r.id,
            device_id=r.device_id,
            timestamp=r.timestamp.isoformat(),
            temperature_c=r.temperature_c,
            vibration_g=r.vibration_g,
            received_at=r.received_at.isoformat() if r.received_at else "",
        )
        for r in readings
    ]

    return DeviceStatusResponse(device_id=device_id, alert=alert, recent_readings=recent)


# ── GET /health ─────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health_check():
    return {"status": "healthy", "service": "FactorySense"}
