import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from kitchen.simulator import generate, LABELS
from kitchen.features import add_features, rule_based


def test_simulator_shapes_and_labels():
    df = generate(hours=2, seed=3)
    assert len(df) == 7200
    assert set(df.label) <= set(LABELS)
    assert df[["temp_c", "humidity", "lpg_ppm", "aq_ppm"]].notna().all().all()


def test_features_are_causal():
    """Changing the future must not change past features."""
    df = generate(hours=1, seed=4)
    f1 = add_features(df).iloc[:1000]
    df2 = df.copy()
    df2.loc[1500:, "lpg_ppm"] = 99999
    f2 = add_features(df2).iloc[:1000]
    pd.testing.assert_frame_equal(f1, f2)


def test_rules_catch_obvious_leak():
    df = generate(hours=1, seed=5)
    df.loc[100:200, ["lpg_ppm", "flame", "temp_c", "aq_ppm"]] = [3000, 0, 28, 450]
    assert (rule_based(df).iloc[100:200] == "gas_leak").all()


def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("KITCHEN_DB", str(tmp_path / "k.db"))
    import importlib
    import kitchen.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    h = {"X-Api-Key": api.API_KEY, "X-Device-Id": "t1"}
    assert c.post("/api/telemetry", json={"temp_c": 30, "humidity": 50, "lpg_ppm": 100, "aq_ppm": 400,
                                          "motion": True}, headers={"X-Api-Key": "wrong"}).status_code == 401
    state = None
    for i in range(200):   # steadily rising LPG with no heat
        r = c.post("/api/telemetry", json={"temp_c": 29, "humidity": 55, "lpg_ppm": 150 + 15 * i,
                                           "aq_ppm": 420, "motion": False, "container_g": 1500 - i},
                   headers=h).json()
        state = r["state"]
    assert state == "gas_leak"
    assert "FAN:1" in r["commands"]
    assert c.get("/api/alerts").json()[0]["kind"] == "gas_leak"
    assert c.get("/api/inventory?device=t1").json()["level_g"] is not None
