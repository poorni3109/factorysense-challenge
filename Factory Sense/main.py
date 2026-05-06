import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import DeviceState

logger = logging.getLogger("factory_sense.alert_engine")

# Thresholds
TEMP_THRESHOLD_C     = 75.0
TEMP_CONSECUTIVE     = 3
VIBE_THRESHOLD_G     = 2.5
VIBE_CONSECUTIVE     = 5
SILENCE_THRESHOLD_S  = 120
SILENCE_CHECK_INTERVAL = 30

_twilio_client = None
_twilio_from   = None
_twilio_to     = None
_twilio_ready  = False

def init_twilio():
    global _twilio_client, _twilio_from, _twilio_to, _twilio_ready

    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    raw_from = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    raw_to   = os.getenv("ALERT_WHATSAPP_TO")

    # Force "whatsapp:" prefix if missing
    _twilio_from = raw_from if raw_from.startswith("whatsapp:") else f"whatsapp:{raw_from}"
    if raw_to:
        _twilio_to = raw_to if raw_to.startswith("whatsapp:") else f"whatsapp:{raw_to}"

    if sid and token and _twilio_to:
        try:
            from twilio.rest import Client
            _twilio_client = Client(sid, token)
            _twilio_ready = True
            logger.info("✅ Twilio initialized — From: %s | To: %s", _twilio_from, _twilio_to)
        except Exception as e:
            logger.warning("⚠️ Twilio init failed (%s)", e)
    else:
        logger.warning("⚠️ Twilio not configured properly — using console fallback")

def _send_whatsapp(body: str) -> bool:
    if _twilio_ready and _twilio_client:
        try:
            msg = _twilio_client.messages.create(body=body, from_=_twilio_from, to=_twilio_to)
            logger.info("📱 WhatsApp sent (SID: %s)", msg.sid)
            return True
        except Exception as e:
            logger.error("❌ WhatsApp failed: %s", e)
            return False
    else:
        logger.info("📱 [CONSOLE] %s", body)
        return True

def send_alert_message(device_id: str, alert_type: str):
    icons = {"temperature": "🌡️ HIGH TEMP", "vibration": "📳 HIGH VIBRATION", "silence": "🔇 DEVICE SILENT"}
    label = icons.get(alert_type, f"⚠️ {alert_type.upper()}")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_whatsapp_alert_logic(f"🚨 FACTORY ALERT — {label}\nDevice: {device_id}\nType: {alert_type}\nTime: {ts}\nAction: Inspect immediately.")

def send_resolved_message(device_id: str, alert_type: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    send_whatsapp_alert_logic(f"✅ ALERT RESOLVED\nDevice: {device_id}\nCleared: {alert_type}\nTime: {ts}\nStatus: Back to normal.")

def send_whatsapp_alert_logic(body):
    # Wrapper to handle the actual sending
    _send_whatsapp(body)

def _get_or_create_state(db: Session, device_id: str) -> DeviceState:
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
    return state

def check_alerts(db: Session, device_id: str, temperature_c: float = None, vibration_g: float = None, silence: bool = False) -> dict:
    state = _get_or_create_state(db, device_id)
    if silence:
        return _handle_silence(state, db)

    if temperature_c is not None:
        state.consecutive_temp_breaches = state.consecutive_temp_breaches + 1 if temperature_c > TEMP_THRESHOLD_C else 0
    if vibration_g is not None:
        state.consecutive_vibe_breaches = state.consecutive_vibe_breaches + 1 if vibration_g > VIBE_THRESHOLD_G else 0
    
    state.last_seen = datetime.now(timezone.utc)
    
    breach = (state.consecutive_temp_breaches >= TEMP_CONSECUTIVE) or (state.consecutive_vibe_breaches >= VIBE_CONSECUTIVE)
    b_type = "temperature" if state.consecutive_temp_breaches >= TEMP_CONSECUTIVE else "vibration" if breach else None
    
    res = _evaluate_transition(state, breach, b_type)
    db.commit()
    return res

def _handle_silence(state, db):
    if state.alert_state == 0:
        state.alert_state, state.alert_type, state.updated_at = 1, "silence", datetime.now(timezone.utc)
        send_alert_message(state.device_id, "silence")
        db.commit()
        return {"transition": "alert", "alert_type": "silence"}
    return {"transition": "suppressed"}

def _evaluate_transition(state, breach, breach_type):
    if breach and state.alert_state == 0:
        state.alert_state, state.alert_type, state.updated_at = 1, breach_type, datetime.now(timezone.utc)
        send_alert_message(state.device_id, breach_type)
        return {"transition": "alert", "alert_type": breach_type}
    if not breach and state.alert_state == 1:
        old_type = state.alert_type
        state.alert_state, state.alert_type, state.updated_at = 0, None, datetime.now(timezone.utc)
        send_resolved_message(state.device_id, old_type)
        return {"transition": "resolved", "alert_type": old_type}
    return {"transition": "suppressed" if breach else "normal"}

async def silent_failure_checker():
    await asyncio.sleep(10)
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, _check_silent_devices)
        except Exception as e:
            logger.error("Silence loop error: %s", e)
        await asyncio.sleep(SILENCE_CHECK_INTERVAL)

def _check_silent_devices():
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=SILENCE_THRESHOLD_S)
        stale = db.query(DeviceState).filter(DeviceState.last_seen < cutoff, DeviceState.alert_state == 0).all()
        for dev in stale:
            check_alerts(db, dev.device_id, silence=True)
    finally:
        db.close()
