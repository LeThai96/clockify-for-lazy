# Clockify For Lazy

Automate Clockify time entries with randomized work reasons and realistic schedules.

## Features

- Create `2-4` (configurable) time entries per day
- Exact daily total (default: `8 hours`)
- Times aligned to `STEP_MINUTES` (default: 5-minute grid)
- Skip days that already have records
- Skip weekends
- Configurable `DAY_OFF` and `PUBLIC_HOLIDAYS`
- On `DAY_OFF`: create one non-billable entry (`Annual leave`, 09:00–17:00)
- On `PUBLIC_HOLIDAYS`: create one non-billable entry (`Public holiday`)
- On regular workdays: generated entries are billable
- Support single date or date range backfill
- Review a month of Clockify records against env rules
- Saved review runs (GUID) can overwrite incorrect days
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

### Overwrite mis-logged day-off dates

If a `DAY_OFF` date already has work entries, `--overwrite` deletes those entries and creates the `Annual leave` record. Other days are left as they are. Preview first with `--dry-run`:

```powershell
python scripts/clockify_daily.py --date 2026-04-21 --overwrite --dry-run
python scripts/clockify_daily.py --start-date 2026-04-20 --end-date 2026-04-24 --overwrite
```

To replace only leave days, limit `--date` or the range to those `DAY_OFF` dates.

### Add tags to existing entries

```powershell
python scripts/clockify_tag_existing.py --start-date 2026-04-01 --end-date 2026-04-30
```

Use `--dry-run` first to preview tag updates without sending PATCH requests.

### Review a month

Checks Clockify entries against the same schedule rules as the daily script (`TIMEZONE`, work window, entry bounds, `TOTAL_MINUTES`, `STEP_MINUTES`, `DESCRIPTIONS`, `DAY_OFF`, `PUBLIC_HOLIDAYS`, project, tags). Default month is the current month in `TIMEZONE`. Future days in that month are not reviewed.

Each run prints a GUID and writes `reviews/<guid>.json` (gitignored). Incorrect workdays store a generated replacement set; leave and holiday days store the exact expected entry. Weekend leftovers store deletes only.

```powershell
python scripts/clockify_review.py
python scripts/clockify_review.py --month 2026-04
python scripts/clockify_review.py --overwrite 11111111-1111-1111-1111-111111111111 --dry-run
python scripts/clockify_review.py --overwrite 11111111-1111-1111-1111-111111111111
```

`--overwrite` loads that saved run and does not re-review. It deletes the current entries on each incorrect day, then creates the stored replacements. Correct days are left as they are. Do not pass `--month` together with `--overwrite`.

## Important Date Rules

- Use either:
  - `--date` (or `CLOCKIFY_TARGET_DATE`)
  - **or** `--start-date` + `--end-date` (or env equivalents)
- Do not combine single-date and range options.
- Weekends are skipped.
- `DAY_OFF` creates one non-billable entry with description `Annual leave` from 09:00 to 17:00.
- `PUBLIC_HOLIDAYS` creates one non-billable entry with description `Public holiday`.
- Public holiday entry length uses `TOTAL_MINUTES` starting at `WORKDAY_START`.
- `--overwrite` replaces existing entries only on `DAY_OFF` dates.
- The review CLI uses `--month YYYY-MM` (default: current month). Do not combine `--month` with `--overwrite`.

## GitHub Actions Daily Automation

Use `.github/workflows/clockify-daily.yml`.

Set repository secrets:

- `CLOCKIFY_API_KEY`
- `CLOCKIFY_WORKSPACE_ID`
- `CLOCKIFY_PROJECT_ID` (optional)

Set repository variables under **Settings → Secrets and variables → Actions → Variables**. Each variable is optional. If it is missing, the workflow uses the default below.

| Variable | Default |
| --- | --- |
| `TIMEZONE` | `Asia/Bangkok` |
| `WORKDAY_START` | `09:00` |
| `WORKDAY_END` | `18:00` |
| `MIN_ENTRIES` | `2` |
| `MAX_ENTRIES` | `4` |
| `MIN_MINUTES` | `30` |
| `MAX_MINUTES` | `120` |
| `PUBLIC_HOLIDAYS` | empty (comma- or newline-separated `YYYY-MM-DD`) |

`PUBLIC_HOLIDAYS` is a repository variable, so changing the holiday list does not require a code change. Example:

```text
2026-01-01,2026-04-30,2026-05-01,2026-09-01,2026-09-02
```

One date per line also works. `.env` is not on the runner; local runs read `PUBLIC_HOLIDAYS` from `.env`, and Actions reads the variable.

The schedule stays in the workflow file as `0 5 * * *` (05:00 UTC, 12:00 Asia/Bangkok). GitHub Actions does not expand variables inside `cron`, so changing the run time still means editing that line.

For daily automation, do not set `CLOCKIFY_TARGET_DATE`, `CLOCKIFY_START_DATE`, or `CLOCKIFY_END_DATE`.

## Testing

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

