"""Cloud backend for the smart kitchen.

    uvicorn kitchen.api:app --host 0.0.0.0 --port 8000

POST /api/telemetry   – ESP8266 (or simulator) pushes one reading per second
GET  /api/latest      – latest reading + current state + hazard probabilities
GET  /api/history     – last N minutes of readings
GET  /api/alerts      – alert log
GET  /api/inventory   – smart-container level and days-until-empty estimate
GET  /                – live dashboard
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from threadpoolctl import threadpool_limits

from .features import add_features

ROOT = Path(__file__).resolve().parents[1]
API_KEY = os.environ.get("KITCHEN_API_KEY", "change-me")
DB_PATH = os.environ.get("KITCHEN_DB", str(ROOT / "kitchen.db"))
MODEL_PATH = ROOT / "models" / "hazard_detector.joblib"
HAZARDS = {"gas_leak": "LPG leak detected – close the regulator, do not switch anything on",
           "fire": "Fire / smoke detected – evacuate and use extinguisher",
           "unattended": "Stove on with nobody in the kitchen for 5+ minutes"}
BUFFER = 1800                     # seconds of history kept per device for features
CONFIRM = 10                      # consecutive agreeing predictions before alerting
ALERT_COOLDOWN = 120              # seconds between repeated alerts of the same type
CONTAINER_FULL_G = 2000.0


class Reading(BaseModel):
    temp_c: float = Field(ge=-20, le=120)
    humidity: float = Field(ge=0, le=100)
    lpg_ppm: float = Field(ge=0, le=100000)
    aq_ppm: float = Field(ge=0, le=100000)
    motion: bool
    flame: bool = False
    container_g: float | None = None


class Engine:
    def __init__(self, db_path=DB_PATH):
        self.lock = threading.Lock()
        self.buf = defaultdict(lambda: deque(maxlen=BUFFER))
        self.recent = defaultdict(lambda: deque(maxlen=CONFIRM))
        self.last_alert = {}
        self.state = {}
        bundle = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None
        self.model = bundle["model"] if bundle else None
        self.features = bundle["features"] if bundle else None
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry(ts REAL, device TEXT, temp_c REAL, humidity REAL,
                lpg_ppm REAL, aq_ppm REAL, motion INT, flame INT, container_g REAL, state TEXT);
            CREATE INDEX IF NOT EXISTS ix_t ON telemetry(device, ts);
            CREATE TABLE IF NOT EXISTS alerts(ts REAL, device TEXT, kind TEXT, message TEXT, prob REAL);
        """)

    def classify(self, device):
        df = pd.DataFrame(self.buf[device])
        if self.model is None:
            return "normal", {}
        f = add_features(df).iloc[[-1]][self.features]
        # one row per call: OpenMP thread start-up inside FastAPI's worker
        # threads costs far more than the prediction itself
        with threadpool_limits(1):
            proba = self.model.predict_proba(f)[0]
        probs = dict(zip(self.model.classes_, proba.round(3).tolist()))
        return max(probs, key=probs.get), probs

    def ingest(self, device: str, r: Reading):
        now = time.time()
        row = r.model_dump()
        row["motion"], row["flame"] = int(r.motion), int(r.flame)
        with self.lock:
            self.buf[device].append(row)
            raw_state, probs = self.classify(device)
            self.recent[device].append(raw_state)
            confirmed = raw_state if len(set(self.recent[device])) == 1 and \
                len(self.recent[device]) == CONFIRM else self.state.get(device, {}).get("state", "normal")
            commands, alert = [], None
            if confirmed in HAZARDS:
                commands.append("FAN:1")
                key = (device, confirmed)
                if now - self.last_alert.get(key, 0) > ALERT_COOLDOWN:
                    self.last_alert[key] = now
                    alert = {"ts": now, "device": device, "kind": confirmed, "message": HAZARDS[confirmed],
                             "prob": probs.get(confirmed, 1.0)}
                    self.db.execute("INSERT INTO alerts VALUES(?,?,?,?,?)", tuple(alert.values()))
            elif confirmed == "normal" and r.aq_ppm < 800 and r.lpg_ppm < 500:
                commands.append("FAN:0")
            self.state[device] = {"ts": now, "state": confirmed, "probs": probs, "reading": row}
            self.db.execute("INSERT INTO telemetry VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (now, device, r.temp_c, r.humidity, r.lpg_ppm, r.aq_ppm, row["motion"],
                             row["flame"], r.container_g, confirmed))
            self.db.commit()
        if alert:
            notify(alert)
        return {"state": confirmed, "commands": commands, "alert": alert}

    def inventory(self, device):
        rows = self.db.execute("SELECT ts, container_g FROM telemetry WHERE device=? AND container_g IS NOT NULL "
                               "ORDER BY ts DESC LIMIT 20000", (device,)).fetchall()
        if len(rows) < 30:
            return {"level_g": None}
        ts, g = np.array(rows[::-1], dtype=float).T  # oldest -> newest
        level = float(np.median(g[-10:]))
        slope = np.polyfit(ts - ts[0], g, 1)[0]          # grams / second
        days_left = None if slope >= -1e-6 else level / (-slope) / 86400
        return {"level_g": round(level, 1), "percent": round(100 * level / CONTAINER_FULL_G, 1),
                "usage_g_per_day": round(-slope * 86400, 1),
                "days_left": None if days_left is None else round(days_left, 1),
                "reorder": bool(level < 0.2 * CONTAINER_FULL_G or (days_left is not None and days_left < 3))}


def notify(alert):
    """Push to Telegram if configured (TELEGRAM_TOKEN + TELEGRAM_CHAT_ID)."""
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(f"[ALERT] {alert['device']}: {alert['message']}")
    if token and chat:
        try:
            import httpx
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": f"🚨 {alert['device']}: {alert['message']}"}, timeout=5)
        except Exception as e:  # never let a notification failure break ingestion
            print("telegram failed:", e)


app = FastAPI(title="Smart Kitchen IoT")
engine = Engine()


def _auth(key):
    if key != API_KEY:
        raise HTTPException(401, "bad api key")


@app.post("/api/telemetry")
def telemetry(r: Reading, x_api_key: str = Header(...), x_device_id: str = Header("kitchen-01")):
    _auth(x_api_key)
    return engine.ingest(x_device_id, r)


@app.get("/api/latest")
def latest(device: str | None = None):
    if not engine.state:
        return {}
    device = device or next(iter(engine.state))
    return {"device": device, **engine.state.get(device, {})}


@app.get("/api/history")
def history(device: str | None = None, minutes: int = 10):
    device = device or (next(iter(engine.state)) if engine.state else "")
    cur = engine.db.execute("SELECT ts,temp_c,humidity,lpg_ppm,aq_ppm,motion,state FROM telemetry "
                            "WHERE device=? AND ts>=? ORDER BY ts", (device, time.time() - 60 * minutes))
    cols = ["ts", "temp_c", "humidity", "lpg_ppm", "aq_ppm", "motion", "state"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/api/alerts")
def alerts(limit: int = 50):
    cur = engine.db.execute("SELECT ts,device,kind,message,prob FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(zip(["ts", "device", "kind", "message", "prob"], r)) for r in cur.fetchall()]


@app.get("/api/inventory")
def inventory(device: str | None = None):
    device = device or (next(iter(engine.state)) if engine.state else "")
    return engine.inventory(device)


@app.get("/")
def dashboard():
    return FileResponse(ROOT / "kitchen" / "static" / "dashboard.html")
