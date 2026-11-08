# ✚ AHEAD — Advanced Health Early Awareness and Disease Detection System

**Senior Design Project** · Python · Streamlit · scikit-learn · XGBoost · Plotly · Gemini

AHEAD is an educational early-awareness screening platform. A user (or a clinician) enters
lifestyle, medical-history and clinical values and receives a machine-learning screening
estimate for **diabetes**, **cardiovascular disease** or **chronic kidney disease** — together
with a gauge, next steps, the factors AHEAD noticed in the inputs and an optional AI-generated
explanation.

> AHEAD does not diagnose disease and must not be used to start, stop or change treatment.

---

## Table of contents

1. [Features](#features)
2. [Project structure](#project-structure)
3. [Setup](#setup)
4. [Running the app](#running-the-app)
5. [Demo accounts](#demo-accounts)
6. [AI features (Gemini)](#ai-features-gemini)
7. [Datasets](#datasets)
8. [Re-training the models](#re-training-the-models)
9. [How the machine learning works](#how-the-machine-learning-works)
10. [Deploying on Render + Neon](#deploying-on-render--neon)
11. [Common errors](#common-errors)

---

## Features

| Area | What it does |
|---|---|
| **Sign-in** | Patient, Doctor and Admin accounts; you stay signed in after a reload; sign-in limits; public About / Contact / Help pages |
| **Admin** | Accounts & Security page: add doctors with one-time set-up links, deactivate/reactivate (signs out at once), reset links, audit log. No health data |
| **Overview** | Welcome hero with quick-start, KPI cards, session risk distribution, dataset and model-quality charts |
| **Screenings** | Guided form per condition, six selectable model families (recommended first), gauge, next steps, risk-factor chips, urgent-care notice, AHEAD Insight (Gemini), model interpretation. Diabetes estimates whether HbA1c is likely to be in the prediabetes range or higher; kidney asks short lifestyle and health questions with kidney tests optional; cardiovascular compares answers with survey respondents who reported a heart attack (not a 10-year risk). Every result is sorted into *See a doctor soon*, *At risk* or *Not flagged* (`ahead/triage.py`) |
| **Data & Analytics** | Dataset explorer, final test metrics, threshold sweep, algorithm comparison table |
| **Clinical Dashboard** *(doctors)* | Review CSV/XLSX rows for missing/invalid values, derive age from DOB, correct incomplete records, score a cohort, record clinician decisions, and download CSV/PDF summaries |
| **AI Assistant** | "Ask AHEAD" chat with suggested prompts; answers via Gemini (offline answers for the suggestions) |
| **Settings** | Persistent profile/password, Light/Dark appearance, session history controls and account actions |

### Patient wellness and linked care (experimental)

The patient navigation also includes **Health & Lifestyle Tracker** (transparent five-part lifestyle score,
dated check-ins, trends, personal goals, manual 30×30 activity log), **Food Nutrition Analyzer**
(photo preview, optional real Gemini image estimate, mandatory manual confirmation, meal history),
**My plan** (the prevention plan a linked doctor approves, with progress from the tracker, plus
doctor-assigned medicines and taken/skipped/unlogged dose tracking),
**Notifications** (in-app reminders when the website is open), and an optional browser-speech **Voice Assistant**.
Wellness is independent of the three disease models. Numeric wellness rules are educational heuristics, not
validated clinical scales. A food photo is never analyzed without a configured Gemini key; manual entry
remains available. The photo is sent to Google's model only when the user presses Analyze Food and is not
stored by AHEAD. Nutrition estimates must be checked and confirmed by the patient. Voice speech recognition
depends on browser support and is limited to short read-only answers; page buttons provide reliable navigation.

Patients explicitly add a doctor's account email under **Settings → Preferences → Care team** to grant
that doctor access to their account records; they can revoke access there. A separate unchecked permission
allows that doctor to send selected linked records to Gemini for an optional AI pre-consultation draft.
The draft must be verified against the source records. The doctor-only **Advanced
Analytics & Patient Intelligence** area reports only linked patients, their dated saved assessments,
medication adherence, and record-based summaries. Doctors create/edit/pause/resume/stop medication plans;
patients can only log their own scheduled doses. The original **Clinical Dashboard** handles imported
de-identified files separately. Imported rows are not automatically merged with registered patient accounts.
The default record-based summary does not use generative AI. Voice questions are
asked through the microphone in the **AI Assistant** chat bar: Gemini transcribes the recording (it is not
stored), page requests such as "open medication" and the patient's own status questions are answered inside
the app, and answers can be read aloud by the browser. The voice and photo flows have explicit fallback paths
and make no diagnostic or prescribing decisions.

Database changes are versioned SQL files in `ahead/migrations/` (`001_initial.sql`, `002_…`). They run
automatically when the app starts, each one once, and are recorded in the `schema_migrations` table. With
`DATABASE_URL` set the app uses PostgreSQL (the live site uses Neon); without it, a local SQLite file
(`AHEAD_DB_PATH`, default `ahead.sqlite3`). Make a Neon backup branch before deploying a new migration.
`GEMINI_API_KEY` (or `GOOGLE_API_KEY`) enables optional food photo analysis and the AI assistant/insights.
Do not enter real patient data: AHEAD is tested with test users only, and real patient data would need
UAE hosting and ethics approval first (UAE Federal Law No. 2 of 2019).
In-app reminders are calculated when pages are visited; there are no background jobs, push, SMS, or email.
Times in medication scheduling use `Asia/Dubai`; historical timestamps are stored in UTC.

---

## Project structure

```
SDP-Project-AHEAD/
├── app.py                  ← entry point: page config, routing, sidebar
├── ahead/
│   ├── config.py           ← disease registry, form layout, labels, navigation, demo accounts
│   ├── resources.py        ← cached models / metadata / datasets / images / Gemini client
│   ├── clinical_data.py    ← row-by-row upload validation and DOB calculation
│   ├── storage.py          ← local SQLite accounts and doctor review queue
│   ├── reports.py          ← downloadable individual screening PDFs
│   ├── theme.py            ← colour tokens (light & dark), global CSS, Plotly styling
│   ├── components.py       ← shared UI pieces + session-state helpers
│   ├── screening.py        ← the guided screening flow (form → prediction → guidance)
│   └── pages/
│       ├── public.py       ← login, about, contact, help
│       ├── overview.py     ← dashboard
│       ├── screenings.py   ← condition picker + predictor
│       ├── analytics.py    ← data & model explorer
│       ├── clinical.py     ← doctor-only cohort screening
│       ├── assistant.py    ← Ask AHEAD chat
│       └── settings.py     ← settings tabs
├── train_model.py          ← benchmarks six model families per disease, saves models + metadata
├── scripts/build_nhanes_training.py ← builds the NHANES diabetes and kidney training files
├── scripts/missing_inputs.py ← measures every model with optional model inputs left blank
├── login.css               ← styles for the public (pre-login) pages
├── assets/                 ← favicon and SVG icons (inlined as data URIs)
├── static/                 ← large background images, served by Streamlit at app/static/…
├── data/                   ← nhanes_diabetes.csv · nhanes_kidney.csv · heart.csv · nhanes/ (source extract)
├── models/
│   ├── <disease>_model.pkl ← recommended pipeline per disease
│   ├── all_models/         ← every benchmarked pipeline (<disease>_<model>.pkl)
│   ├── model_meta.json     ← features, metrics, thresholds, benchmark results
│   └── missing_inputs.json ← test metrics and thresholds for blank optional inputs
├── .streamlit/config.toml  ← Streamlit theme + server settings
└── requirements.txt
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.12 or 3.13 is recommended.

Without `DATABASE_URL` the app keeps its data in a local SQLite file (`ahead.sqlite3`, or `AHEAD_DB_PATH`).
That is for development only; the live site uses PostgreSQL on Neon (see [Deploying](#deploying-on-render--neon)).

---

## Running the app

```bash
streamlit run server.py
```

Open the printed URL (usually `http://localhost:8501`). `server.py` is the Streamlit app plus three small HTTP
routes (`/healthz`, `/auth/session`, `/auth/logout`) that keep you signed in across reloads with an HttpOnly
cookie. (`streamlit run app.py` also works for quick UI work, but then a reload signs you out.)

Create a patient account on the sign-in page. For the first admin, run
`python scripts/create_account.py --email you@example.com --name "Your Name" --role Admin`; it asks for the
password (shown as `*`). The admin then adds doctors from **Accounts & Security**.

**Tests:** `python -m pytest -q` runs everything on throw-away SQLite files. To run the same tests on PostgreSQL,
set `AHEAD_TEST_DATABASE_URL` to a *local* database (the tests refuse any other host, so they can never touch Neon).

---

## Demo accounts

| Role | Email | Password |
|---|---|---|
| Patient | `user@ahead.demo` | `user123` |
| Doctor | `doctor@ahead.demo` | `doctor123` |
| Admin | `admin@ahead.demo` | `admin123` |

For a **local** classroom run only: set `AHEAD_ENABLE_DEMO_ACCOUNTS=1` (`run_windows.bat` does). They are never
created on PostgreSQL, so they can't exist on the live site. Passwords are stored as salted PBKDF2 hashes.

Clinical uploads use the form's columns. Age and the optional columns (lab values, readings and the heart history questions) may be left out. An optional `DOB`, `DateOfBirth`, `date_of_birth`, or `Date of Birth` column (YYYY-MM-DD) fills age or the heart model's age category. Missing and invalid values appear per row and are not scored until corrected. Correct a row and save it in the review queue, then score eligible records. Clinicians can mark a record reviewed or excluded and add a note. The file is limited to 5,000 rows and 15 MB. The PDF and CSV contain entered data; handle downloads appropriately.

The app is English only. No email, text or push notifications are sent. The map-search link does not show live appointment availability.

---

## AI features (Gemini)

AHEAD Insight (personalised result explanation) and the AI Assistant use Google Gemini
through the `google-genai` SDK. Provide an API key in **either** place:

```bash
export GEMINI_API_KEY="your-key"          # environment variable
```

or in `.streamlit/secrets.toml` (git-ignored):

```toml
GEMINI_API_KEY = "your-key"
```

Without a key the app still works: Insight shows a notice and the Assistant answers the
suggested questions from built-in text. The model name lives in `ahead/config.py`
(`GEMINI_MODEL`).

---

## Datasets

All three models are trained on real CDC survey data; nothing is generated or synthetic. Sources,
licences, label definitions, held-out results and limitations are in
[`docs/data_sources.md`](docs/data_sources.md).

| Condition | File | Source | Records | Target (1 means) | Test set |
|---|---|---|---|---|---|
| Diabetes | `data/nhanes_diabetes.csv` | CDC NHANES 1999-2023 (public domain) | 53,903 | `hba1c_57_or_higher`: HbA1c ≥ 5.7 % at the exam, in adults never told they have diabetes | 2021-2023 cycle (4,966) |
| Cardiovascular | `data/heart.csv` | CDC BRFSS 2022 via Kaggle, Kamil Pytlak (CC0) | 246,022 | `HadHeartAttack`: ever told they had a heart attack (a past event) | random 20 % (49,205) |
| Kidney health | `data/nhanes_kidney.csv` | CDC NHANES 1999-2023 (public domain) | 60,396 | `kidney_marker`: eGFR < 60 or urine ACR ≥ 30 mg/g at one visit | 2021-2023 cycle (5,551) |

25.8 % of diabetes rows, 16.8 % of kidney rows and 5.5 % of heart rows are positive, which is why the app
reports sensitivity, specificity, precision and ROC-AUC alongside accuracy. Lab values are never inputs to
the diabetes or kidney models (they define the labels); the app checks them with rules instead.

The two NHANES files are built from CDC's raw public-use files: `scripts/fetch_nhanes.py` downloads them,
`scripts/rebuild_nhanes.py` merges and cleans them into `data/nhanes/nhanes_1999_2023_research.csv.gz` (with
CDC's recommended creatinine corrections) and `scripts/build_nhanes_training.py` writes the training files. The earlier `diabetes.csv` and
`kidney_disease.csv` were removed: the kidney file was synthetic and the diabetes file's origin and licence
were unknown.

| Condition | Recommended model | Threshold | Test AUC | Sensitivity | Specificity | Precision |
|---|---|---|---|---|---|---|
| Diabetes | Logistic regression | 0.21 | 0.735 | 86.0 % | 48.3 % | 39.6 % |
| Cardiovascular | Logistic regression | 0.05 | 0.828 | 79.8 % | 71.0 % | 13.7 % |
| Kidney health | Logistic regression | 0.22 | 0.776 | 66.2 % | 77.5 % | 37.7 % |

For comparison, a partial ADA diabetes risk score reaches a test AUC of 0.722 on the same diabetes test rows.

---

## Re-training the models

The trained pipelines are included. To rebuild everything:

```bash
python scripts/fetch_nhanes.py            # CDC NHANES files into data/nhanes/raw/ (~283 MB, not committed)
python scripts/rebuild_nhanes.py          # data/nhanes/nhanes_1999_2023_research.csv.gz
python scripts/build_nhanes_training.py   # data/nhanes_diabetes.csv, data/nhanes_kidney.csv
python train_model.py
python scripts/missing_inputs.py
```

For each disease `train_model.py`:

1. splits the data: for diabetes and kidney the 2021-2023 NHANES cycle is the test set and the earlier
   cycles are split 80 % train / 20 % validation (stratified); for heart it is a stratified
   64 % train / 16 % validation / 20 % test,
2. trains Random Forest, Logistic Regression, Gradient Boosting, SVM, Decision Tree and XGBoost
   inside a `Pipeline(preprocessor → classifier)`, without class re-weighting so scores stay close to
   observed rates,
3. saves every pipeline to `models/all_models/` so the app can compare them,
4. recommends Logistic Regression for every condition (its score moves one way per answer and can be
   explained; the gap to the best family's validation ROC-AUC is recorded),
5. tunes each model's probability threshold on the validation set to maximise Youden's J
   (sensitivity + specificity − 1),
6. reports final metrics and a calibration check once on the untouched test set and writes
   `models/model_meta.json` (including the data source, label definition, population and split).

`scripts/missing_inputs.py` measures every saved model with each allowed pattern of optional model inputs
left blank and writes `models/missing_inputs.json`. No current model has optional model inputs, so the file
lists none.

---

## How the machine learning works

* **Preprocessing** — numeric columns are median-imputed and standardised; categorical columns
  are most-frequent-imputed and one-hot encoded (`ColumnTransformer`).
* **Prediction** — `predict_proba()` gives the model's score for the condition; the result is
  flagged when it reaches the chosen model's own threshold, tuned on the validation set for every benchmark model.
  The heart label is a past heart attack, so the heart score is close to the share of BRFSS respondents
  with similar answers who reported one; it is not a 10-year or future risk.
* **Heart history** — the heart form also asks about a past heart attack and angina (`HadHeartAttack`,
  `HadAngina`; never model inputs). A Yes to either, or to a past stroke (`HadStroke`, also a model input),
  puts the result on *See a doctor soon* whatever the score, and no prevention plan is offered for it.
  Heart screenings and uploads without these answers still score; a blank answer is not checked.
* **Lab values** — HbA1c, glucose, creatinine, eGFR and urine ACR are not model inputs; entered values
  are checked by the rules in `ahead/triage.py`, and a measured value counts for more than the model's
  estimate: diabetes HbA1c ≥ 6.5 % or glucose ≥ 200 mg/dL → *See a doctor soon*, HbA1c 5.7–6.4 % → *At risk*,
  HbA1c < 5.7 % → *Not flagged*; kidney eGFR < 60 or ACR ≥ 30 mg/g → *See a doctor soon* (repeat the test soon
  to rule out a short-term change, and at about 3 months to confirm it), eGFR ≥ 60 with ACR < 30 mg/g → *Not
  flagged*. Without these labs the model decides. eGFR is
  worked out from serum creatinine with the 2021 race-free CKD-EPI equation when only creatinine is entered.
* **Older saved records** — kidney screenings and uploads saved with the retired kidney form keep their keys
  (`config.LEGACY_FIELDS`); they still display, sort into a track and print, but are never re-scored. Their HbA1c
  (≥ 6.5 %) and fasting blood sugar (≥ 126 mg/dL) still make them *See a doctor soon* (`triage.LEGACY_RULES`).
* **Interpretation** — tree models expose `feature_importances_`, Logistic Regression uses
  absolute coefficient magnitude; RBF-SVM has no per-feature importance.
* **Rule-based factors** — independent of the model, the app highlights entered values that are
  commonly discussed with a clinician (e.g. HbA1c ≥ 5.7 %, BMI ≥ 30, eGFR < 60).

---

## Deploying on Render + Neon

The live site runs on **Render** (web service, free plan, Frankfurt) with **Neon** PostgreSQL (Frankfurt).
`render.yaml` describes the service; Render wakes it on the first visit after 15 idle minutes (about a minute).

1. **Neon:** create a project `ahead` (region Frankfurt). Copy the connection string (Dashboard → Connect).
2. **Render:** New → Blueprint → pick this repository and branch. When asked, paste the Neon string into
   `DATABASE_URL`, and add `GEMINI_API_KEY` if you use the AI features. Secrets live only in Render.
3. After the first deploy, set `AHEAD_PUBLIC_URL` to the site address (used in set-up and reset links).
4. **First admin** (from your computer, once): set `DATABASE_URL` in your terminal to the Neon string, then run
   `python scripts/create_account.py --email … --name … --role Admin`. Sign in with **Doctor / Admin** selected.
5. Migrations run on every start. Before a deploy that adds a migration, create a Neon backup branch.

Health check: `GET /healthz` returns `{"status": "ok", "database": "ok"}`.

---

## Common errors

| Error | Fix |
|---|---|
| "No trained model is available" | Run `python train_model.py` to create `models/` |
| `ModuleNotFoundError` | Activate the virtual environment and `pip install -r requirements.txt` |
| A selected model says "has not been saved yet" | Run `python train_model.py` to generate `models/all_models/` |
| AI Assistant / Insight unavailable | Provide `GEMINI_API_KEY` (see above) |
| Background images missing | `server.enableStaticServing = true` must stay in `.streamlit/config.toml` and the PNGs in `static/` |
| Signed out after refreshing the page | Start with `streamlit run server.py` (not `app.py`). Sessions also end after 2 hours idle or 12 hours in total |
| Blank page after editing | Hard-refresh the browser (`Ctrl/Cmd + Shift + R`) |
