# TfL Line Monitor (Azure Functions)

This project is an Azure Functions app that checks Transport for London (TfL) Underground line status and records disruption periods in Azure Table Storage.  
It is designed to detect when service changes from **Good Service** to a delay state, track how long the disruption lasts, and store that history for later reporting.

## What this project does

- Monitors these lines on a timer:
  - Metropolitan
  - Bakerloo
  - Hammersmith & City
  - Circle
- Runs every 2 minutes (`0 */2 * * * *`)
- Skips weekend execution (Saturday/Sunday)
- Calls the TfL line status API for each line
- Writes status history to a shared `TFLState` table
- Writes delay events to per-line delay tables:
  - `MetropolitanLineDelays` (or `TABLE_NAME` app setting)
  - `BakLineDelays`
  - `H&CDelays`
  - `CircleDelays`

## Delay tracking behavior

For each line, the function records:

- A **new delay** when status moves from `Good Service` to a delay type (`Minor Delays`, `Severe Delays`, `Part Suspended`, `Planned Closure`)
- A **delay end** when status returns to `Good Service` (including calculated `duration_minutes`)
- A **severity change** by closing the previous delay record and opening a new one

Stored fields include timestamps, severity, reason text, and affected stops.

## Requirements

- Python 3.x
- Azure Functions Core Tools
- Azure subscription + existing Function App
- Azure Storage account connection string

Python package dependencies are listed in `requirements.txt`:

- `azure-functions`
- `azure-data-tables`
- `requests`

## Configuration (App Settings)

Set these in Azure Function App settings (and in `local.settings.json` for local development):

- `STORAGE_CONNECTION_STRING` (required)
- `TABLE_NAME` (optional; used by Metropolitan monitor, default `MetropolitanLineDelays`)

> Important: `local.settings.json` contains local secrets and should not be committed.

## Local development quick start

1. Create and activate a Python virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure `local.settings.json` with valid local settings.
4. Start the Functions host:
   ```bash
   func start
   ```

## Command Palette reminder: deploy updates to Azure (`tfl-metropolitan-monitor`)

When you update this script and want to push changes to Azure from VS Code:

1. Open Command Palette (`Cmd+Shift+P` on macOS / `Ctrl+Shift+P` on Windows/Linux)
2. Run: **Azure Functions: Deploy to Function App...**
3. Select function app: **`tfl-metropolitan-monitor`**
4. Confirm deployment when prompted
5. (Optional) Run: **Azure Functions: Sync Triggers** after deployment

## Publishing this repo to GitHub

This workspace is already set up as a git repository and already has an `origin` remote configured. Before publishing:

1. Review files with `git status`
2. Commit your changes
3. Create a GitHub repository
4. Push to GitHub:
   ```bash
   git branch -M main
   git push -u origin main
   ```

## Train Timeliness Monitor (Proof of Concept)

A separate module (`train_timeliness.py`) calculates real-time on-time performance for monitored lines by comparing TFL arrival predictions against scheduled timetable data.

### How it works

1. **Fetches arrival predictions** from `GET /Line/{id}/Arrivals` — real-time predicted arrival times for trains at monitored stations
2. **Fetches timetable data** from `GET /Line/{id}/Timetable/{stationId}` — the scheduled departure times
3. **Matches each prediction** to the nearest scheduled time (within a 15-minute window)
4. **Calculates variance** — positive = late, negative = early, within ±120 seconds = on time
5. **Persists snapshots** as timestamped JSON files for historical tracking
6. **Generates an HTML report** with:
   - Per-line on-time percentage with ✅/⚠️/❌ indicators
   - SVG sparkline showing on-time % over time
   - Stacked distribution bar (early / on time / late)
   - Station-level breakdown table

### Running locally

```bash
pip install -r requirements.txt
python train_timeliness.py
```

This creates `timeliness_data/` (JSON snapshots) and `timeliness_report.html`.

A sample `timeliness_report.html` generated from synthetic timetable/prediction data is checked into the repo for quick reference to the layout and metrics. Running `python train_timeliness.py` against the live TfL API will regenerate the report and populate `timeliness_data/` with real snapshots.

### Isolation from `tfl-metropolitan-monitor`

Timeliness logic is intentionally **not** wired into `/home/runner/work/TFLStatus/TFLStatus/function_app.py` and does not add timer/HTTP triggers to the existing status-monitor Function App.

To keep deployments isolated, run timeliness as a separate process:

- locally via `python train_timeliness.py`, or
- via `.github/workflows/train-timeliness.yml` (scheduled GitHub Actions workflow).

### Running tests

```bash
python -m unittest tests.test_train_timeliness -v
```
