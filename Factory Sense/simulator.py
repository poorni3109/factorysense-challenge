"""
simulator.py — ESP32 Device Simulator

Simulates 3 ESP32 sensors posting to POST /telemetry every 10 seconds.

Device Behavior:
  ESP32-001: Normal operation throughout (temp 40-65°C, vibe 0.5-1.8g)
  ESP32-002: Normal operation throughout (temp 45-70°C, vibe 0.3-2.0g)
  ESP32-003: Systematic fault sequence triggering ALL THREE alert types:
    Phase 1 (0-30s):     Normal baseline readings
    Phase 2 (30-70s):    HIGH TEMP >75°C — triggers temp alert (3+ consecutive)
    Phase 3 (70-100s):   Normal temp — RESOLVES temp alert
    Phase 4 (100-170s):  HIGH VIBE >2.5g — triggers vibe alert (5+ consecutive)
    Phase 5 (170-200s):  Normal vibe — RESOLVES vibe alert
    Phase 6 (200-335s):  SILENCE (135s, no data) — triggers silence alert
    Phase 7 (335s+):     Resume normal — RESOLVES silence alert

Total runtime: ~6 minutes.
"""

import time
import random
import threading
import logging
import sys
from datetime import datetime, timezone

import requests

# ── Config ──────────────────────────────────────────────────────

API_URL = "http://127.0.0.1:8000"
INTERVAL = 10  # seconds between readings (matches ESP32 real cadence)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-12s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("simulator")


# ── HTTP Helper ─────────────────────────────────────────────────

def post_reading(device_id: str, temp: float, vibe: float):
    """Send one telemetry reading to the server."""
    payload = {
        "device_id": device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "temperature_c": round(temp, 2),
        "vibration_g": round(vibe, 3),
    }
    try:
        r = requests.post(f"{API_URL}/telemetry", json=payload, timeout=5)
        r.raise_for_status()
        data = r.json()
        transition = data.get("alert_transition", "?")
        icon = {"normal": "🟢", "alert": "🔴", "suppressed": "🟡", "resolved": "🔵"}.get(transition, "⚪")
        log.info("%s %s │ temp=%6.1f°C │ vibe=%5.2fg │ → %s", icon, device_id, temp, vibe, transition)
    except requests.ConnectionError:
        log.error("❌ %s │ Cannot connect — is the server running?", device_id)
    except Exception as e:
        log.error("❌ %s │ %s", device_id, e)


# ── Device 1 & 2: Normal Operation ─────────────────────────────

def run_normal_device(device_id: str, temp_range: tuple, vibe_range: tuple, stop: threading.Event):
    """Continuously send normal readings until stopped."""
    log.info("🟢 %s — started (normal)", device_id)
    while not stop.is_set():
        post_reading(device_id, random.uniform(*temp_range), random.uniform(*vibe_range))
        stop.wait(INTERVAL)
    log.info("🛑 %s — stopped", device_id)


# ── Device 3: Fault Sequence ───────────────────────────────────

def run_faulty_device(device_id: str, stop: threading.Event):
    """
    Systematically trigger all 3 alert types in sequence:
      Temp Alert → Resolve → Vibe Alert → Resolve → Silence Alert → Resolve
    """
    log.info("🔴 %s — started (FAULT SEQUENCE)", device_id)
    t0 = time.time()

    while not stop.is_set():
        elapsed = time.time() - t0

        # ── Phase 1: Normal baseline (0-30s) ───────────────────
        if elapsed < 30:
            log.info("📊 %s │ Phase 1: NORMAL (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(45, 65), random.uniform(0.5, 1.5))
            stop.wait(INTERVAL)

        # ── Phase 2: High Temp (30-70s) — 4 readings → alert on 3rd ───
        elif elapsed < 70:
            log.info("🌡️  %s │ Phase 2: HIGH TEMP (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(80, 95), random.uniform(0.5, 1.5))
            stop.wait(INTERVAL)

        # ── Phase 3: Normal (70-100s) — temp drops → resolved ──
        elif elapsed < 100:
            log.info("✅ %s │ Phase 3: NORMAL — resolving temp (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(45, 65), random.uniform(0.5, 1.5))
            stop.wait(INTERVAL)

        # ── Phase 4: High Vibe (100-170s) — 7 readings → alert on 5th ─
        elif elapsed < 170:
            log.info("📳 %s │ Phase 4: HIGH VIBE (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(45, 65), random.uniform(3.0, 4.5))
            stop.wait(INTERVAL)

        # ── Phase 5: Normal (170-200s) — vibe drops → resolved ─
        elif elapsed < 200:
            log.info("✅ %s │ Phase 5: NORMAL — resolving vibe (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(45, 65), random.uniform(0.5, 1.5))
            stop.wait(INTERVAL)

        # ── Phase 6: Silence (200-335s) — 135s gap → silence alert ─
        elif elapsed < 335:
            remaining = 335 - elapsed
            log.info("🔇 %s │ Phase 6: SILENCE (%.0fs remaining)", device_id, remaining)
            stop.wait(min(15, remaining))

        # ── Phase 7: Resume normal (335s+) → resolves silence ──
        else:
            log.info("🔵 %s │ Phase 7: RESUMED NORMAL (%.0fs)", device_id, elapsed)
            post_reading(device_id, random.uniform(45, 65), random.uniform(0.5, 1.5))
            stop.wait(INTERVAL)
            if elapsed > 370:
                log.info("🏁 %s │ Fault sequence complete!", device_id)
                break

    log.info("🛑 %s — stopped", device_id)


# ── Main ────────────────────────────────────────────────────────

def main():
    print()
    log.info("=" * 72)
    log.info("🏭  FactorySense — Device Simulator")
    log.info("=" * 72)
    log.info("Target: %s │ Interval: %ds", API_URL, INTERVAL)
    log.info("")
    log.info("  ESP32-001: Normal operation")
    log.info("  ESP32-002: Normal operation")
    log.info("  ESP32-003: FAULT SEQUENCE")
    log.info("    Phase 1:   0-30s    Normal baseline")
    log.info("    Phase 2:  30-70s    HIGH TEMP   → triggers temp alert")
    log.info("    Phase 3:  70-100s   Normal      → resolves temp alert")
    log.info("    Phase 4: 100-170s   HIGH VIBE   → triggers vibe alert")
    log.info("    Phase 5: 170-200s   Normal      → resolves vibe alert")
    log.info("    Phase 6: 200-335s   SILENCE     → triggers silence alert")
    log.info("    Phase 7: 335-370s   Resume      → resolves silence alert")
    log.info("")
    log.info("Total runtime: ~6 minutes. Ctrl+C to stop early.")
    log.info("=" * 72)
    print()

    # Connectivity check
    try:
        r = requests.get(f"{API_URL}/health", timeout=3)
        r.raise_for_status()
        log.info("✅ Server is reachable!")
    except Exception:
        log.error("❌ Cannot reach %s — start the server first:", API_URL)
        log.error("   uvicorn main:app --reload")
        sys.exit(1)

    stop = threading.Event()
    threads = [
        threading.Thread(target=run_normal_device,
                         args=("ESP32-001", (40, 65), (0.5, 1.8), stop), daemon=True),
        threading.Thread(target=run_normal_device,
                         args=("ESP32-002", (45, 70), (0.3, 2.0), stop), daemon=True),
        threading.Thread(target=run_faulty_device,
                         args=("ESP32-003", stop), daemon=True),
    ]

    for t in threads:
        t.start()

    try:
        threads[2].join(timeout=420)  # wait for Device 3 to finish
        log.info("🏁 Device 3 complete — stopping all devices...")
        stop.set()
        for t in threads:
            t.join(timeout=5)
    except KeyboardInterrupt:
        log.info("\n⛔ Ctrl+C — stopping...")
        stop.set()
        for t in threads:
            t.join(timeout=5)

    print()
    log.info("=" * 72)
    log.info("🏁 Simulation complete!")
    log.info("=" * 72)
    log.info("Verify status:")
    log.info("  curl %s/devices/ESP32-001/status", API_URL)
    log.info("  curl %s/devices/ESP32-002/status", API_URL)
    log.info("  curl %s/devices/ESP32-003/status", API_URL)
    log.info("  Swagger UI: %s/docs", API_URL)
    print()


if __name__ == "__main__":
    main()
