"""Train the hazard classifier and compare it against fixed thresholds.

Split by EPISODE (GroupShuffleSplit) so no part of an event leaks between
train and test. Reports per-class F1, detection latency for hazards and
false alarms per 24 h.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import GroupShuffleSplit

from .features import add_features, rule_based
from .simulator import LABELS

ROOT = Path(__file__).resolve().parents[1]
HAZARDS = ["gas_leak", "fire", "unattended"]
CONFIRM = 10  # seconds of agreement before an alert (see tradeoff in the report)


def latency_and_false_alarms(df, pred):
    lat, missed = {h: [] for h in HAZARDS}, {h: 0 for h in HAZARDS}
    for ep, g in df.groupby("episode"):
        for h in HAZARDS:
            on = np.where(g["label"].values == h)[0]
            if len(on) == 0:
                continue
            hit = np.where(pred.loc[g.index].values[on[0]:] == h)[0]
            if len(hit):
                lat[h].append(int(hit[0]))
            else:
                missed[h] += 1
    safe = df["label"].isin(["normal", "cooking"])
    # count alarm *onsets* (rising edges) during safe periods
    alarm = pred.isin(HAZARDS) & safe
    onsets = int((alarm & ~alarm.shift(1, fill_value=False)).sum())
    fa_per_day = onsets / (safe.sum() / 86400)
    return ({h: {"median_s": float(np.median(v)) if v else None, "episodes": len(v) + missed[h],
                 "missed": missed[h]} for h, v in lat.items()}, round(fa_per_day, 2))


def main():
    df = pd.read_csv(ROOT / "data" / "kitchen_sensor_dataset.csv.gz", parse_dates=["ts"])
    X = add_features(df)
    y = df["label"]
    tr, te = next(GroupShuffleSplit(1, test_size=0.3, random_state=42).split(X, y, df["episode"]))
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08, max_leaf_nodes=31,
                                         class_weight="balanced", random_state=0)
    # consecutive seconds are near-duplicates: train on every 5th second
    tr_sub = tr[::5]
    clf.fit(X.iloc[tr_sub], y.iloc[tr_sub])
    test = df.iloc[te]
    ml = pd.Series(clf.predict(X.iloc[te]), index=test.index)
    rules = rule_based(df).iloc[te]

    def confirm(pred, k):
        """Only raise a state after k identical consecutive predictions."""
        ok = pd.concat([pred.eq(pred.shift(i)) for i in range(1, k)], axis=1).all(axis=1)
        return pred.where(ok, "normal")

    tradeoff = {}
    for k in (3, 5, 10):
        lat, fa = latency_and_false_alarms(test, confirm(ml, k))
        tradeoff[f"confirm_{k}s"] = {"latency": lat, "false_alarms_per_day": fa}
    ml_s = confirm(ml, CONFIRM)

    report = {}
    for name, p in [("ml", ml_s), ("rules", rules)]:
        lat, fa = latency_and_false_alarms(test, p)
        rep = classification_report(test["label"], p, labels=LABELS, output_dict=True, zero_division=0)
        report[name] = {"macro_f1": round(rep["macro avg"]["f1-score"], 4),
                        "per_class_f1": {k: round(rep[k]["f1-score"], 4) for k in LABELS},
                        "detection_latency": lat, "false_alarms_per_day": fa}
    report["confirm_window_tradeoff"] = tradeoff
    report["confusion_ml"] = {"labels": LABELS,
                              "matrix": confusion_matrix(test["label"], ml_s, labels=LABELS).tolist()}
    (ROOT / "models").mkdir(exist_ok=True)
    joblib.dump({"model": clf, "features": list(X.columns)}, ROOT / "models" / "hazard_detector.joblib")
    (ROOT / "results" / "detector_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "confusion_ml"}, indent=2))


if __name__ == "__main__":
    main()
