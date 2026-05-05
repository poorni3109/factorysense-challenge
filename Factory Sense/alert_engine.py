"""
alert_engine.py — The core alert state machine, Twilio integration,
                   and background silent-failure checker.

This single module contains ALL alert logic:
  1. Twilio WhatsApp wrapper (with graceful console fallback)
  2. check_alerts()  — evaluates breach counters + state transitions
  3. silent_failure_checker() — async background loop for 2-min silence

State Machine:
  ┌─────────┐  breach detected   ┌─────────┐
  │ NORMAL  │ ─────────────────► │  ALERT  │  → send ONE WhatsApp alert
  │ state=0 │ ◄───────────────── │ state=1 │  → send ONE "Resolved" msg
  └─────────┘  breach cleared    └─────────┘
                                   │     ▲
                                   │     │
                                   └─────┘  breach continues → SUPPRESS
                                            (do nothing)
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from database import SessionLocal
from models import DeviceState

logger = logging.getLogger("factory_sense.alert_engine")

# ═══════════════════════════════════════════════════════════════
# Thresholds
# ═══════════════════════════════════════════════════════════════

TEMP_THRESHOLD_C     = 75.0    # °C
TEMP_CONSECUTIVE     = 3       # consecutive readings
VIBE_THRESHOLD_G     = 2.5     # g-force
VIBE_CONSECUTIVE     = 5       # consecutive readings
SILENCE_THRESHOLD_S  = 120     # seconds (2 minutes)
SILENCE_CHECK_INTERVAL = 30    # background loop interval


# ═══════════════════════════════════════════════════════════════
# 1. Twilio WhatsApp Wrapper
# ═══════════════════════════════════════════════════════════════

_twilio_client = None
_twilio_from   = None
_twilio_to     = None
_twilio_ready  = False


def init_twilio():
    """Initialize Twilio client from env vars. Call once at startup."""
    global _twilio_client, _twilio_from, _twilio_to, _twilio_ready

    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    _twilio_from = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    _twilio_to   = os.getenv("ALERT_WHATSAPP_TO")

    if sid and token and _twilio_to:
        try:
            from twilio.rest import Client
            _twilio_client = Client(sid, token)
            _twilio_ready = True
            logger.info("✅ Twilio initialized — alerts → %s", _twilio_to)
        except Exception as e:
            logger.warning("⚠️  Twilio init failed (%s) — console fallback", e)
    else:
        missing = [k for k, v in {
            "TWILIO_ACCOUNT_SID": sid,
            "TWILIO_AUTH_TOKEN": token,
            "ALERT_WHATSAPP_TO": _twilio_to,
        }.items() if not v]
        logger.warning("⚠️  Twilio not configured (missing: %s) — console fallback", ", ".join(missing))


def _send_whatsapp(body: str) -> bool:
    """Send WhatsApp message, or log to console if Twilio not configured."""
    if _twilio_ready and _twilio_client:
        try:
            msg = _twilio_client.messages.create(
                body=body, from_=_twilio_from, to=_twilio_to,
            )
            logger.info("📱 WhatsApp sent (SID: %s): %s", msg.sid, body)
            return True
        except Exception as e:
            logger.error("❌ WhatsApp failed (%s) — body: %s", e, body)
            return False
    else:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        logger.info("📱 [CONSOLE] [%s] %s", ts, body)
        return True


def send_alert_message(device_id: str, alert_type: str):
    """Send a single ALERT WhatsApp message."""
    icons = {"temperature": "🌡️ HIGH TEMP", "vibration": "📳 HIGH VIBRATION", "silence": "🔇 DEVICE SILENT"}
    label = icons.get(alert_type, f"⚠️ {alert_type.upper()}")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _send_whatsapp(
        f"🚨 FACTORY ALERT — {label}\n"
        f"Device: {device_id}\n"
        f"Type: {alert_type}\n"
        f"Time: {ts}\n"
        f"Action required: Inspect device immediately."
    )


def send_resolved_message(device_id: str, alert_type: str):
    """Send a single RESOLVED WhatsApp message."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _send_whatsapp(
        f"✅ ALERT RESOLVED\n"
        f"Device: {device_id}\n"
        f"Previous alert: {alert_type}\n"
        f"Time: {ts}\n"
        f"Device returned to normal operation."
    )


# ═══════════════════════════════════════════════════════════════
# 2. Alert Evaluation (called on every POST /telemetry)
# ═══════════════════════════════════════════════════════════════

def _get_or_create_state(db: Session, device_id: str) -> DeviceState:
    """Fetch the DeviceState row, or create one for a new device."""
    state = db.query(DeviceState).filter(DeviceState.device_id == device_id).first()
    if state is None:
        state = DeviceState(
            device_id=device_id,
            alert_state=0,
            consecutive_temp_breaches=0,
            consecutive_vibe_breaches=0,
            last_seen=datetime.now(timezone.utc),
        )
        db.add(state)
        db.flush()
        logger.info("📋 New device registered: %s", device_id)
    return state


def check_alerts(db: Session, device_id: str,
                 temperature_c: float = None, vibration_g: float = None,
                 silence: bool = False) -> dict:
    """
    Core alert logic — called after every telemetry POST and by
    the background silent-failure checker.

    Returns dict with:
      transition: "normal" | "alert" | "suppressed" | "resolved"
      alert_type:  str | None
      message:     human-readable description
    """
    state = _get_or_create_state(db, device_id)

    # ── Silence check (from background worker) ─────────────────
    if silence:
        result = _handle_silence(state)
        db.commit()
        return result

    # ── Update counters from new reading ───────────────────────
    if temperature_c is not None:
        if temperature_c > TEMP_THRESHOLD_C:
            state.consecutive_temp_breaches += 1
        else:
            state.consecutive_temp_breaches = 0

    if vibration_g is not None:
        if vibration_g > VIBE_THRESHOLD_G:
            state.consecutive_vibe_breaches += 1
        else:
            state.consecutive_vibe_breaches = 0

    state.last_seen = datetime.now(timezone.utc)

    # ── Check if any breach threshold is crossed ───────────────
    breach_detected = False
    breach_type = None

    if state.consecutive_temp_breaches >= TEMP_CONSECUTIVE:
        breach_detected = True
        breach_type = "temperature"
    elif state.consecutive_vibe_breaches >= VIBE_CONSECUTIVE:
        breach_detected = True
        breach_type = "vibration"

    # ── State machine transitions ──────────────────────────────
    result = _evaluate_transition(state, breach_detected, breach_type)
    db.commit()
    return result


def _handle_silence(state: DeviceState) -> dict:
    """Handle silent-failure: Normal → Alert(silence), or suppress."""
    if state.alert_state == 0:
        state.alert_state = 1
        state.alert_type = "silence"
        state.updated_at = datetime.now(timezone.utc)
        logger.warning("🔇 SILENCE ALERT: %s — no data for %ds+", state.device_id, SILENCE_THRESHOLD_S)
        send_alert_message(state.device_id, "silence")
        return {
            "transition": "alert",
            "alert_type": "silence",
            "message": f"Device {state.device_id} silent for {SILENCE_THRESHOLD_S}s+. Alert sent.",
        }
    else:
        return {
            "transition": "suppressed",
            "alert_type": state.alert_type,
            "message": f"Device {state.device_id} still silent. Suppressed.",
        }


def _evaluate_transition(state: DeviceState, breach: bool, breach_type: str | None) -> dict:
    """Evaluate the state machine: Normal ↔ Alert."""

    # Normal + breach → Alert  (send ONE alert)
    if breach and state.alert_state == 0:
        state.alert_state = 1
        state.alert_type = breach_type
        state.updated_at = datetime.now(timezone.utc)
        logger.warning(
            "🚨 ALERT: %s — %s (temp_streak=%d, vibe_streak=%d)",
            state.device_id, breach_type,
            state.consecutive_temp_breaches,
            state.consecutive_vibe_breaches,
        )
        send_alert_message(state.device_id, breach_type)
        return {"transition": "alert", "alert_type": breach_type,
                "message": f"Alert triggered: {state.device_id} ({breach_type})"}

    # Alert + breach continues → Suppress (do nothing)
    if breach and state.alert_state == 1:
        logger.debug("🔕 SUPPRESS: %s — still in breach", state.device_id)
        return {"transition": "suppressed", "alert_type": state.alert_type,
                "message": f"Suppressed: {state.device_id} still in breach"}

    # Alert + no breach → Normal  (send ONE resolved)
    if not breach and state.alert_state == 1:
        old_type = state.alert_type
        state.alert_state = 0
        state.alert_type = None
        state.updated_at = datetime.now(timezone.utc)
        logger.info("✅ RESOLVED: %s — %s cleared", state.device_id, old_type)
        send_resolved_message(state.device_id, old_type)
        return {"transition": "resolved", "alert_type": old_type,
                "message": f"Resolved: {state.device_id} ({old_type} cleared)"}

    # Normal + no breach → Normal (everything is fine)
    return {"transition": "normal", "alert_type": None,
            "message": f"Normal: {state.device_id} operating normally"}


# ═══════════════════════════════════════════════════════════════
# 3. Background Silent-Failure Checker
# ═══════════════════════════════════════════════════════════════

def _check_silent_devices():
    """Sync function that queries for stale devices. Runs in executor."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=SILENCE_THRESHOLD_S)
        stale = db.query(DeviceState).filter(DeviceState.last_seen < cutoff).all()

        if stale:
            logger.info("🔍 Silence check: %d device(s) silent >%ds", len(stale), SILENCE_THRESHOLD_S)

        for device in stale:
            logger.warning("🔇 %s last seen %s (cutoff %s)", device.device_id, device.last_seen, cutoff)
            check_alerts(db, device.device_id, silence=True)
    except Exception as e:
        logger.error("❌ Silence checker error: %s", e, exc_info=True)
    finally:
        db.close()


async def silent_failure_checker():
    """
    Async coroutine — runs forever as a background task.
    Every SILENCE_CHECK_INTERVAL seconds, scans for devices
    that haven't sent data in SILENCE_THRESHOLD_S seconds.
    """
    logger.info(
        "🔄 Silent-failure checker started (every %ds, threshold %ds)",
        SILENCE_CHECK_INTERVAL, SILENCE_THRESHOLD_S,
    )
    await asyncio.sleep(10)  # initial grace period

    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, _check_silent_devices)
        except Exception as e:
            logger.error("❌ Silence loop error: %s", e, exc_info=True)
        await asyncio.sleep(SILENCE_CHECK_INTERVAL)
