"""
Independent evaluation of AHEAD's three saved models on their held-out test sets (for the report).

Uses exactly the test rows train_model.py held out (same split code) and the models the app loads
(models/<disease>_model.pkl). Adds what the training summary doesn't have:

- 95% confidence intervals (1,000 bootstrap resamples of the test set) for AUC, sensitivity, specificity
- the confusion matrix at the app's threshold
- calibration by tenths of the score (does a 30% score mean about 30%?)
- results by sex and age band (fairness)
- simple baselines on the same rows, with a paired bootstrap for the AUC difference:
    diabetes: partial ADA risk test (Bang et al. 2009; see scripts/build_nhanes_training.py), age alone
    kidney:   count of the risk factors screening guidance names (age 60+, diabetes, high blood pressure,
              heart disease), age alone
    heart:    count of classic risk factors (age 55+, male, smoker ever, diabetes, BMI 30+, no exercise),
              age alone

Run:  python scripts/evaluate_models.py      ->  docs/evaluation/results.json
"""

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from train_model import DISEASE_CONFIGS, load_training_frame, split_data  # noqa: E402

OUT = ROOT / "docs" / "evaluation" / "results.json"
META = json.loads((ROOT / "models" / "model_meta.json").read_text())
N_BOOT = 1000
RNG = np.random.default_rng(2026)


def heart_age(series):
    """BRFSS 'Age 45 to 49' -> 45 (lower edge of the band)."""
    return series.str.extract(r"(\d+)")[0].astype(float)


def baselines(disease, X):
    if disease == "diabetes":
        from build_nhanes_training import ada_points, load_source, TEST_CYCLE
        src = load_source()
        src = src[(src["diabetes"] == 0) & src["hba1c"].notna() & (src["cycle"] == TEST_CYCLE)]
        # Same filter and order as the training file; check row by row before trusting the join.
        if len(src) != len(X) or not np.allclose(src["age"].to_numpy(float), X["age"].to_numpy(float)) \
                or not np.allclose(src["bmi"].fillna(-1).to_numpy(), X["bmi"].fillna(-1).to_numpy()):
            raise SystemExit("diabetes: research rows don't line up with the test rows")
        return {"Partial ADA risk test": ada_points(src).to_numpy(float), "Age alone": X["age"].to_numpy(float)}
    if disease == "kidney":
        count = ((X["age"] >= 60).astype(int) + X["diabetes"].fillna(0) + X["hypertension"].fillna(0)
                 + X["heart_disease"].fillna(0))
        return {"Risk-factor count": count.to_numpy(float), "Age alone": X["age"].to_numpy(float)}
    age = heart_age(X["AgeCategory"])
    count = ((age >= 55).astype(int) + (X["Sex"] == "Male").astype(int)
             + (X["SmokerStatus"] != "Never smoked").astype(int) + (X["HadDiabetes"] == "Yes").astype(int)
             + (X["BMI"] >= 30).astype(int) + (X["PhysicalActivities"] == "No").astype(int))
    return {"Risk-factor count": count.to_numpy(float), "Age alone": age.to_numpy(float)}


def rates(y, flag):
    tp = int(((flag == 1) & (y == 1)).sum()); fn = int(((flag == 0) & (y == 1)).sum())
    fp = int(((flag == 1) & (y == 0)).sum()); tn = int(((flag == 0) & (y == 0)).sum())
    return tp, fn, fp, tn


def ci(values):
    lo, hi = np.percentile(values, [2.5, 97.5])
    return [round(float(lo), 4), round(float(hi), 4)]


def evaluate(disease):
    config = DISEASE_CONFIGS[disease]
    df = load_training_frame(disease)
    _, _, X, _, _, y = split_data(df, disease)
    y = y.to_numpy().astype(int)
    model = joblib.load(ROOT / "models" / f"{disease}_model.pkl")
    p = model.predict_proba(X[config["features"]])[:, 1]
    threshold = META[disease]["decision_threshold"]
    flag = (p >= threshold).astype(int)
    base = baselines(disease, X)

    tp, fn, fp, tn = rates(y, flag)
    n = len(y)
    boot = {"auc": [], "sens": [], "spec": [], **{f"diff:{k}": [] for k in base}, **{f"auc:{k}": [] for k in base}}
    for _ in range(N_BOOT):
        i = RNG.integers(0, n, n)
        yi = y[i]
        if yi.min() == yi.max():
            continue
        a = roc_auc_score(yi, p[i])
        boot["auc"].append(a)
        b_tp, b_fn, b_fp, b_tn = rates(yi, flag[i])
        boot["sens"].append(b_tp / (b_tp + b_fn)); boot["spec"].append(b_tn / (b_tn + b_fp))
        for k, s in base.items():
            ak = roc_auc_score(yi, s[i])
            boot[f"auc:{k}"].append(ak); boot[f"diff:{k}"].append(a - ak)

    auc = roc_auc_score(y, p)
    result = {
        "rows": n, "positives": int(y.sum()), "positive_rate": round(float(y.mean()), 4), "threshold": threshold,
        "label": config["label_definition"],
        "auc": round(float(auc), 4), "auc_ci": ci(boot["auc"]),
        "sensitivity": round(tp / (tp + fn), 4), "sensitivity_ci": ci(boot["sens"]),
        "specificity": round(tn / (tn + fp), 4), "specificity_ci": ci(boot["spec"]),
        "precision": round(tp / (tp + fp), 4), "flagged_share": round(float(flag.mean()), 4),
        "confusion": {"true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn},
        "baselines": [], "calibration": [], "subgroups": [],
    }
    for k, s in base.items():
        diffs = np.array(boot[f"diff:{k}"])
        result["baselines"].append({
            "name": k, "auc": round(float(roc_auc_score(y, s)), 4), "auc_ci": ci(boot[f"auc:{k}"]),
            "difference": round(float(auc - roc_auc_score(y, s)), 4), "difference_ci": ci(diffs),
            # two-sided bootstrap p-value for "no difference"
            "p_value": round(float(min(1.0, 2 * min((diffs <= 0).mean(), (diffs >= 0).mean()))), 4),
        })

    tenth = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False)
    for g in range(10):
        m = (tenth == g).to_numpy()
        result["calibration"].append({"tenth": g + 1, "mean_score": round(float(p[m].mean()), 4),
                                      "observed": round(float(y[m].mean()), 4), "rows": int(m.sum())})

    sex = X["gender"] if "gender" in X else X["Sex"]
    age = X["age"].astype(float) if "age" in X else heart_age(X["AgeCategory"])
    groups = {"Female": (sex == "Female").to_numpy(), "Male": (sex == "Male").to_numpy(),
              "Age 18-39": (age < 40).to_numpy(), "Age 40-59": ((age >= 40) & (age < 60)).to_numpy(),
              "Age 60+": (age >= 60).to_numpy()}
    for name, m in groups.items():
        yg, pg, fg = y[m], p[m], flag[m]
        g_tp, g_fn, g_fp, g_tn = rates(yg, fg)
        aucs = []
        idx = np.flatnonzero(m)
        for _ in range(300):
            j = RNG.choice(idx, len(idx))
            if y[j].min() != y[j].max():
                aucs.append(roc_auc_score(y[j], p[j]))
        result["subgroups"].append({
            "group": name, "rows": int(m.sum()), "positive_rate": round(float(yg.mean()), 4),
            "auc": round(float(roc_auc_score(yg, pg)), 4), "auc_ci": ci(aucs),
            "sensitivity": round(g_tp / max(g_tp + g_fn, 1), 4), "specificity": round(g_tn / max(g_tn + g_fp, 1), 4),
            "flagged_share": round(float(fg.mean()), 4),
            "mean_score": round(float(pg.mean()), 4), "observed": round(float(yg.mean()), 4),
        })
    return result


if __name__ == "__main__":
    results = {"bootstrap_resamples": N_BOOT, "seed": 2026, "models": {}}
    for disease in ("diabetes", "kidney", "heart"):
        print(f"Evaluating {disease}...", flush=True)
        results["models"][disease] = evaluate(disease)
        r = results["models"][disease]
        print(f"  AUC {r['auc']} {r['auc_ci']}  sens {r['sensitivity']} spec {r['specificity']}")
        for b in r["baselines"]:
            print(f"  vs {b['name']}: {b['auc']} diff {b['difference']} {b['difference_ci']} p={b['p_value']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1))
    print(f"Saved {OUT.relative_to(ROOT)}")
