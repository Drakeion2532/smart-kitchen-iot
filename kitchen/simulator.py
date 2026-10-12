"""Physics-inspired kitchen sensor simulator.

Generates 1 Hz telemetry identical in shape to what the Arduino firmware
sends, with labelled episodes:

    normal      – idle kitchen, people passing through
    cooking     – stove on: temperature/humidity rise, mild VOCs, frequent motion
    gas_leak    – LPG concentration rising (knob left open / pipe leak), no heat
    fire        – smoke + heat spike, flame sensor may or may not see it
    unattended  – stove on but nobody in the kitchen for a long time

Real-sensor effects included: Gaussian noise, MQ baseline drift with humidity,
DHT11 1 °C quantisation, PIR false triggers and dropouts, flame-sensor misses.

Used for (1) the labelled dataset in data/, (2) live demo streaming to the API.
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

LABELS = ["normal", "cooking", "gas_leak", "fire", "unattended"]


@dataclass
class State:
    temp: float = 27.0
    hum: float = 55.0
    lpg: float = 120.0
    aq: float = 420.0
    container: float = 2000.0


def _episode(kind: str, secs: int, rng: np.random.Generator, st: State, amb: float):
    rows = []
    leak_rate = rng.uniform(1.0, 6.0)         # ppm/s growth of a leak
    fire_start = rng.integers(int(secs * 0.2), int(secs * 0.5))
    cook_heat = rng.uniform(6, 14)            # °C rise above ambient while cooking
    presence = rng.uniform(0.25, 0.6)         # P(motion) per second while cooking
    for t in range(secs):
        target_t, target_h, target_lpg, target_aq = amb, 55, 120, 420
        motion_p, flame = 0.04, False
        label = kind
        if kind == "cooking":
            target_t, target_h, target_aq = amb + cook_heat, 68, 650
            motion_p = presence
            if rng.random() < 0.004:           # a burner "puff" while lighting
                st.lpg += rng.uniform(150, 400)
            st.container -= rng.uniform(0, 0.15) if rng.random() < 0.05 else 0
        elif kind == "unattended":
            target_t, target_h, target_aq = amb + cook_heat + min(t / 120, 10), 66, 700 + 2 * t
            motion_p = 0.0 if t > 30 else presence
            label = "cooking" if t < 300 else "unattended"   # becomes dangerous after 5 min alone
        elif kind == "gas_leak":
            target_lpg = 120 + leak_rate * t
            motion_p = 0.08
            label = "normal" if t < 20 else "gas_leak"
        elif kind == "fire":
            if t >= fire_start:
                k = t - fire_start
                target_t, target_aq, target_h = amb + min(4 + k * 0.35, 45), 900 + 40 * k, 45
                flame = rng.random() < 0.6                    # IR sensor misses some frames
                motion_p = 0.15
                label = "fire" if k >= 5 else "normal"
            else:
                label = "normal"
        # first-order dynamics toward the target + noise
        st.temp += 0.03 * (target_t - st.temp) + rng.normal(0, 0.08)
        st.hum += 0.02 * (target_h - st.hum) + rng.normal(0, 0.3)
        st.lpg += 0.15 * (target_lpg - st.lpg) + rng.normal(0, 8)
        st.aq += 0.05 * (target_aq - st.aq) + rng.normal(0, 10)
        drift = 0.6 * (st.hum - 55)            # MQ sensors read high in humid air
        motion = rng.random() < motion_p or rng.random() < 0.005
        if rng.random() < 0.02:
            motion = False                     # PIR dropout
        rows.append({
            "temp_c": float(np.round(st.temp)),             # DHT11 resolution is 1 °C
            "humidity": float(np.clip(np.round(st.hum), 20, 95)),
            "lpg_ppm": round(max(0.0, st.lpg + drift), 1),
            "aq_ppm": round(max(300.0, st.aq + drift), 1),
            "motion": int(motion), "flame": int(flame),
            "container_g": round(st.container + rng.normal(0, 2), 1),
            "label": label,
        })
    # recover toward baseline afterwards
    st.lpg, st.aq = min(st.lpg, 400), min(st.aq, 700)
    st.temp = st.temp - 0.6 * (st.temp - amb)
    return rows


def generate(hours: float = 48, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    st, rows, t0 = State(), [], pd.Timestamp("2025-01-01 06:00:00")
    total = int(hours * 3600)
    kinds = ["normal", "cooking", "gas_leak", "fire", "unattended"]
    probs = [0.38, 0.30, 0.11, 0.10, 0.11]
    ep = 0
    while len(rows) < total:
        kind = rng.choice(kinds, p=probs)
        secs = {"normal": rng.integers(600, 2400), "cooking": rng.integers(600, 2400),
                "gas_leak": rng.integers(300, 900), "fire": rng.integers(300, 600),
                "unattended": rng.integers(900, 1800)}[kind]
        amb = rng.uniform(24, 34)              # Indian kitchens: warm ambient
        for r in _episode(kind, int(secs), rng, st, amb):
            r["episode"] = ep
            rows.append(r)
        ep += 1
        if st.container < 300:                 # container refilled
            st.container = 2000.0
    df = pd.DataFrame(rows[:total])
    df.insert(0, "ts", t0 + pd.to_timedelta(np.arange(len(df)), unit="s"))
    return df


def stream(url: str, api_key: str, speed: float = 10.0, seed: int = 1):
    """Replay simulated telemetry into the running API (live demo)."""
    import httpx
    df = generate(hours=2, seed=seed)
    with httpx.Client(timeout=5) as c:
        for _, r in df.iterrows():
            payload = r.drop(["label", "episode", "ts"]).to_dict()
            payload["motion"], payload["flame"] = bool(payload["motion"]), bool(payload["flame"])
            c.post(url, json=payload, headers={"X-Api-Key": api_key, "X-Device-Id": "sim-kitchen"})
            time.sleep(1 / speed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--out", default="data/kitchen_sensor_dataset.csv.gz")
    ap.add_argument("--stream", help="API url to stream to, e.g. http://127.0.0.1:8000/api/telemetry")
    ap.add_argument("--api-key", default="change-me")
    ap.add_argument("--speed", type=float, default=10)
    a = ap.parse_args()
    if a.stream:
        stream(a.stream, a.api_key, a.speed)
    else:
        d = generate(a.hours)
        d.to_csv(a.out, index=False)
        print(d.label.value_counts())
        print("saved", a.out, d.shape)
