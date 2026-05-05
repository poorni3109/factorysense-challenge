# DECISIONS.md — Architecture Decision Records

> Every major design choice in FactorySense, with rationale for the 48-hour
> constraint and how we'd architect it for 1,000+ devices.

---

## 1. Data Model / Schema Choices

### Tables

| Table | Purpose | Key Columns |
|---|---|---|
| `telemetry_readings` | Append-only time-series of raw sensor data | `device_id`, `timestamp`, `temperature_c`, `vibration_g`, `received_at` |
| `device_state` | One row per device — tracks alert FSM + breach counters | `device_id` (PK), `alert_state`, `alert_type`, `last_seen`, `consecutive_temp_breaches`, `consecutive_vibe_breaches` |

### Why This Schema?

**`telemetry_readings`** is a classic append-only time-series table. The composite index `(device_id, timestamp)` makes the "last 50 readings for device X" query fast — it's an index-range scan, not a full table scan.

**`device_state`** is the critical design decision. By storing `consecutive_temp_breaches` and `consecutive_vibe_breaches` directly in the row, every incoming reading becomes an **O(1) update** — increment or reset the counter. The alternative (querying the last N readings and scanning for consecutive breaches) would be O(N) per reading.

**Tradeoff**: Inline counters don't survive retroactive re-analysis. If we wanted to ask "what was the alert state at 3am yesterday?", we'd need to replay all readings. For a 48-hour challenge with 3 devices and a live demo, O(1) wins decisively.

### Why SQLite?

SQLite is zero-config — no server process, no connection strings, no Docker containers. `pip install sqlalchemy` and the database is a single file. For 3 devices posting every 10 seconds, SQLite handles the write volume trivially. Its single-writer lock only becomes a problem at ~100+ concurrent writers.

---

## 2. Alert State Machine & Deduplication

### The State Machine

```
                    breach detected
    ┌─────────┐ ─────────────────────► ┌─────────┐
    │ NORMAL  │                        │  ALERT  │  → send ONE WhatsApp
    │ state=0 │ ◄───────────────────── │ state=1 │  → send ONE "Resolved"
    └─────────┘     breach cleared     └─────────┘
                                         │     ▲
                                         │     │
                                         └─────┘
                                     breach continues
                                       → SUPPRESS
                                      (send nothing)
```

### How Deduplication Works

The `device_state.alert_state` column is the single source of truth:

1. **Normal (0) + Breach → Alert (1)**: Flip the state, send ONE alert message. This is the only path that triggers a notification.
2. **Alert (1) + Breach continues → Alert (1)**: The state doesn't change, so nothing happens. This is the deduplication — subsequent breach readings are silently absorbed.
3. **Alert (1) + No breach → Normal (0)**: Flip the state back, send ONE "Resolved" message.
4. **Normal (0) + No breach → Normal (0)**: No-op. Everything is fine.

This is a two-state finite state machine. The transition is the trigger, not the state itself. Because `check_alerts()` is called synchronously inside the POST handler (before the response is sent), there's no race condition — the database row is locked for the duration of the transaction.

### Breach Detection

Consecutive counters are updated on every POST:
- `temperature_c > 75` → `consecutive_temp_breaches += 1`, else reset to 0
- `vibration_g > 2.5` → `consecutive_vibe_breaches += 1`, else reset to 0

Alert triggers when:
- `consecutive_temp_breaches >= 3` (temperature rule)
- `consecutive_vibe_breaches >= 5` (vibration rule)

Temperature is checked first (higher priority). A single normal reading resets the counter, which is the correct behavior — "consecutive" means uninterrupted.

---

## 3. Silent-Failure Detection

### Why It's Hard

Silent failure is fundamentally different from temperature and vibration alerts:

- **Temp/Vibe alerts** are triggered by data that DOES arrive (active detection).
- **Silence alerts** are triggered by data that DOESN'T arrive (passive detection).

A POST endpoint cannot detect the absence of future POSTs. The server must independently poll for staleness.

### Implementation

An `asyncio` background task runs in a perpetual loop:

```python
async def silent_failure_checker():
    while True:
        # Query: SELECT * FROM device_state WHERE last_seen < (now - 120s)
        # For each stale device: call check_alerts(silence=True)
        await asyncio.sleep(30)
```

**Key details:**
- **Runs every 30 seconds** — fast enough to catch a 120-second gap within one check cycle.
- **Uses `run_in_executor`** — the SQLAlchemy query is synchronous, so we run it in a thread pool to avoid blocking FastAPI's async event loop.
- **Spawned via `asyncio.create_task()` in the lifespan** — tied to the server process lifecycle.
- **`last_seen` is updated on every POST** — so the 120-second window is measured from the last received reading, not the last checked time.

### The Race Condition

There's a subtle edge case: the background worker detects silence at the exact moment the device resumes. The silence alert fires, and the very next POST triggers a "resolved." This is correct behavior — the alert was real (the device WAS silent), and the resolution is also real (it's back). Both messages are sent exactly once.

---

## 4. Scaling to 1,000 Devices

### What Breaks First

| Component | Breaking Point | Solution |
|---|---|---|
| **SQLite** | Single-writer lock at ~100 concurrent writes | **PostgreSQL** + connection pooling (PgBouncer) |
| **In-process background task** | Single point of failure, no horizontal scaling | **Celery Beat + Redis** — separate worker process |
| **Synchronous Twilio calls** | Rate-limited at ~1 msg/sec; blocks POST response | **Message queue** (SQS/Redis) with a notification worker |
| **Monolithic check_alerts()** | Tight coupling of ingestion and alerting | **Event-driven**: POST publishes to Kafka/Redis Streams; alert consumer processes independently |

### Proposed Architecture at 1,000 Devices

```
ESP32s ──► Load Balancer ──► FastAPI Cluster (stateless, 3+ instances)
                                 │
                                 ├── Writes to PostgreSQL + TimescaleDB
                                 └── Publishes "reading.ingested" to Redis Streams
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        Alert Worker    Alert Worker    Alert Worker
                        (Celery)        (Celery)        (Celery)
                              │
                              └── Pushes notifications to SQS
                                          │
                                          ▼
                                  Notification Worker
                                  (rate-limited Twilio calls)
```

**Key changes:**
1. **TimescaleDB** for hypertable-based time-series storage with automatic partitioning.
2. **Redis Streams** as an event bus to decouple ingestion from alerting.
3. **Celery workers** for alert processing — horizontally scalable, restartable.
4. **Dedicated notification worker** that respects Twilio rate limits with exponential backoff.
5. **Device state in Redis** (not SQLite) for sub-millisecond reads on the hot path.

### What Stays the Same

The **state machine logic** (`check_alerts`) is identical at any scale. The algorithm doesn't change — only where it runs and how fast the data store is.

---

## 5. Tradeoffs Made for the 48-Hour Constraint

| Decision | What We Did | What We Sacrificed | Why It's OK |
|---|---|---|---|
| **SQLite** | Zero-config file database | Concurrent write throughput | 3 devices × 0.1 writes/sec = trivial |
| **Inline counters** | O(1) breach detection | Historical re-analysis capability | Demo only needs live state |
| **In-process background task** | `asyncio.create_task()` | Fault isolation, horizontal scaling | Server IS the system for a demo |
| **Synchronous Twilio calls** | Direct API call in request path | POST latency (~500ms on alert) | Alerts are rare events (3-4 per demo) |
| **No authentication** | No API keys or JWT | Security | Local demo only, not internet-facing |
| **No rate limiting** | No request throttling | DDoS protection | 3 simulated devices, no adversary |
| **No retry logic** | Twilio call fails → log and move on | Guaranteed delivery | Acceptable for demo; console fallback exists |
| **Flat file structure** | All `.py` files in root | Package organization for large teams | 6 files total; clarity > structure |
| **Threading in simulator** | `threading.Thread` per device | Memory efficiency at 10K devices | 3 threads = ~24MB; negligible |
| **No tests** | Manual verification via simulator | CI/CD confidence | Simulator IS the integration test |

### What I'd Add With 48 More Hours

1. **Unit tests** for `check_alerts()` covering all state transitions
2. **Dockerfile + docker-compose** for one-command deployment
3. **Grafana dashboard** reading from SQLite for real-time visualization
4. **Prometheus metrics** on the FastAPI endpoints
5. **Structured JSON logging** instead of emoji-formatted console output
6. **API authentication** with API keys per device
