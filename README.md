# Clockify For Lazy

Automate Clockify time entries with randomized work reasons and realistic schedules.

## Features

- Create `2-4` (configurable) time entries per day
- Exact daily total (default: `8 hours`)
- Times aligned to `STEP_MINUTES` (default: 5-minute grid)
- Skip days that already have records
- Skip weekends
- Configurable `DAY_OFF` and `PUBLIC_HOLIDAYS`
- On `DAY_OFF`: create one non-billable entry (`Day off`)
- On `PUBLIC_HOLIDAYS`: create one non-billable entry (`Public holiday`)
- On regular workdays: generated entries are billable
- Support single date or date range backfill
- Debug mode to print requests without sending them
- GitHub Actions schedule support

## Requirements

- Python `3.10+`
- Clockify API key
- Clockify workspace ID

## Install

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

The script auto-loads `.env` via `python-dotenv`.

Example `.env`:

```env
CLOCKIFY_API_KEY=your_api_key
CLOCKIFY_WORKSPACE_ID=your_workspace_id
CLOCKIFY_PROJECT_ID=optional_project_id

TIMEZONE=Asia/Bangkok
WORKDAY_START=09:00
WORKDAY_END=18:00

MIN_ENTRIES=3
MAX_ENTRIES=5
MIN_MINUTES=30
MAX_MINUTES=120
TOTAL_MINUTES=480
STEP_MINUTES=5

DAY_OFF=2026-04-07,2026-04-15
PUBLIC_HOLIDAYS=2026-01-01,2026-04-30,2026-05-01
```

## Usage

### Daily run (default mode)

```powershell
python scripts/clockify_daily.py
```

### Single date

```powershell
python scripts/clockify_daily.py --date 2026-04-21
```

### Date range (inclusive)

```powershell
python scripts/clockify_daily.py --start-date 2026-04-06 --end-date 2026-04-21
```

### Dry-run (no POST)

```powershell
python scripts/clockify_daily.py --dry-run
```

### Debug mode (print requests, no HTTP calls)

```powershell
python scripts/clockify_daily.py --debug
```

## Important Date Rules

- Use either:
  - `--date` (or `CLOCKIFY_TARGET_DATE`)
  - **or** `--start-date` + `--end-date` (or env equivalents)
- Do not combine single-date and range options.
- Weekends are skipped.
- `DAY_OFF` creates one non-billable entry with description `Day off`.
- `PUBLIC_HOLIDAYS` creates one non-billable entry with description `Public holiday`.
- For day off/public holiday entry length, script uses `TOTAL_MINUTES` starting at `WORKDAY_START`.

## GitHub Actions Daily Automation

Use `.github/workflows/clockify-daily.yml`.

Set repository secrets:

- `CLOCKIFY_API_KEY`
- `CLOCKIFY_WORKSPACE_ID`
- `CLOCKIFY_PROJECT_ID` (optional)

Then keep workflow command:

```yaml
- name: Create time entries
  run: python scripts/clockify_daily.py
```

For daily automation, do not set `CLOCKIFY_TARGET_DATE`, `CLOCKIFY_START_DATE`, or `CLOCKIFY_END_DATE`.

## Testing

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

