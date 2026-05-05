"""
models.py — SQLAlchemy ORM models and Pydantic schemas.

Two tables:
  1. TelemetryReading  — raw sensor data (append-only time-series)
  2. DeviceState        — one row per device tracking alert status
                          and consecutive breach counters

Design choice: consecutive breach counters live IN the DeviceState row
rather than being computed via windowed queries. This makes every POST
an O(1) update instead of an O(N) scan of recent rows. The tradeoff is
that counters don't survive retroactive re-analysis, but for a 48-hour
challenge this is the right call.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Integer, String, Float, DateTime, Index
from pydantic import BaseModel, Field

from database import Base


# ═══════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════

def _utcnow():
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════
# SQLAlchemy ORM Models
# ═══════════════════════════════════════════════════════════════

class TelemetryReading(Base):
    """Raw sensor data ingested from ESP32 devices (append-only)."""

    __tablename__ = "telemetry_readings"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    device_id     = Column(String(50), nullable=False, index=True)
    timestamp     = Column(DateTime, nullable=False)
    temperature_c = Column(Float, nullable=False)
    vibration_g   = Column(Float, nullable=False)
    received_at   = Column(DateTime, default=_utcnow)

    # Composite index: speeds up "last N readings for device X" queries
    __table_args__ = (
        Index("ix_device_timestamp", "device_id", "timestamp"),
    )

    def __repr__(self):
        return (
            f"<Reading({self.device_id}, "
            f"temp={self.temperature_c}°C, vibe={self.vibration_g}g)>"
        )


class DeviceState(Base):
    """
    One row per device. Tracks the alert state machine and
    consecutive breach counters for O(1) alert evaluation.

    alert_state:
        0 = Normal  — device is operating within thresholds
        1 = Alert   — an active alert has been sent; suppress duplicates

    alert_type:
        "temperature" | "vibration" | "silence" | None
    """

    __tablename__ = "device_state"

    device_id                  = Column(String(50), primary_key=True)
    alert_state                = Column(Integer, default=0, nullable=False)
    alert_type                 = Column(String(50), nullable=True)
    last_seen                  = Column(DateTime, default=_utcnow)
    consecutive_temp_breaches  = Column(Integer, default=0, nullable=False)
    consecutive_vibe_breaches  = Column(Integer, default=0, nullable=False)
    updated_at                 = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    def __repr__(self):
        label = "ALERT" if self.alert_state == 1 else "NORMAL"
        return f"<DeviceState({self.device_id}, {label})>"


# ═══════════════════════════════════════════════════════════════
# Pydantic Schemas (Request / Response)
# ═══════════════════════════════════════════════════════════════

class TelemetryIn(BaseModel):
    """POST /telemetry request body."""
    device_id: str = Field(..., examples=["ESP32-001"])
    timestamp: str = Field(..., examples=["2026-05-05T12:00:00Z"])
    temperature_c: float = Field(..., ge=-40, le=200)
    vibration_g: float = Field(..., ge=0, le=50)


class TelemetryResponse(BaseModel):
    """POST /telemetry response."""
    status: str = "ok"
    device_id: str
    alert_transition: str
    alert_type: Optional[str] = None
    message: str


class AlertInfo(BaseModel):
    """Nested alert info in device status response."""
    alert_state: int
    alert_state_label: str
    alert_type: Optional[str] = None
    last_seen: Optional[str] = None
    consecutive_temp_breaches: int = 0
    consecutive_vibe_breaches: int = 0


class ReadingOut(BaseModel):
    """A single reading in the status response."""
    id: int
    device_id: str
    timestamp: str
    temperature_c: float
    vibration_g: float
    received_at: str

    class Config:
        from_attributes = True


class DeviceStatusResponse(BaseModel):
    """GET /devices/{device_id}/status response."""
    device_id: str
    alert: AlertInfo
    recent_readings: list[ReadingOut]
