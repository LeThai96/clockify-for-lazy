#!/usr/bin/env python3
"""
Create random Clockify time entries for the current calendar day (in a configured timezone).
Skips creation if Clockify already has any time entry for that calendar day (unless overridden).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, TextIO
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

API_BASE = "https://api.clockify.me/api/v1"

DEFAULT_DESCRIPTIONS = [
    "Meeting",
    "Fixing bugs",
    "Analyze requirements",
    "Doing tasks",
    "Code review",
    "Documentation",
    "Planning",
    "Sync with team",
]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {s!r}")
    return h, m


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mask_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]} (redacted)"


def _time_entries_url(
    workspace_id: str,
    user_id: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    page_size: int | None = None,
) -> str:
    parts = [
        f"start={urllib.parse.quote(_utc_iso(start_utc))}",
        f"end={urllib.parse.quote(_utc_iso(end_utc))}",
    ]
    if page_size is not None:
        parts.append(f"page-size={page_size}")
    qs = "&".join(parts)
    return f"{API_BASE}/workspaces/{workspace_id}/user/{user_id}/time-entries?{qs}"


def _debug_print_request(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
    stream: TextIO | None = None,
) -> None:
    # Resolve at call time so pytest/capsys replacements of sys.stdout are respected.
    out = sys.stdout if stream is None else stream
    print(f"{method} {url}", file=out)
    print(f"  X-Api-Key: {_mask_api_key(api_key)}", file=out)
    print("  Content-Type: application/json", file=out)
    print("  User-Agent: clockify-for-lazy/1.0", file=out)
    if body is not None:
        print(f"  Body: {json.dumps(body, indent=2)}", file=out)
    print(file=out)


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "clockify-for-lazy/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {err_body}") from e


def get_user(api_key: str) -> dict[str, Any]:
    url = f"{API_BASE}/user"
    out = _http_json("GET", url, api_key=api_key)
    if not isinstance(out, dict):
        raise RuntimeError("Unexpected /user response")
    return out


def list_time_entries(
    api_key: str,
    workspace_id: str,
    user_id: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    page_size: int | None = None,
) -> list[dict[str, Any]]:
    """GET time entries in [start_utc, end_utc)."""
    url = _time_entries_url(workspace_id, user_id, start_utc, end_utc, page_size=page_size)
    out = _http_json("GET", url, api_key=api_key)
    if isinstance(out, list):
        return out
    raise RuntimeError("Unexpected time-entries list response")


def create_time_entry(
    api_key: str,
    workspace_id: str,
    *,
    start: datetime,
    end: datetime,
    description: str,
    project_id: str | None,
) -> dict[str, Any]:
    url = f"{API_BASE}/workspaces/{workspace_id}/time-entries"
    body: dict[str, Any] = {
        "start": _utc_iso(start),
        "end": _utc_iso(end),
        "description": description,
        "billable": False,
    }
    if project_id:
        body["projectId"] = project_id
    out = _http_json("POST", url, api_key=api_key, body=body)
    if not isinstance(out, dict):
        raise RuntimeError("Unexpected create time entry response")
    return out


@dataclass
class Config:
    api_key: str
    workspace_id: str
    project_id: str | None
    tz: ZoneInfo
    work_start: time
    work_end: time
    min_entries: int
    max_entries: int
    min_minutes: int
    max_minutes: int
    total_minutes: int
    step_minutes: int
    descriptions: list[str]
    ignore_day_has_entries: bool
    dry_run: bool
    debug: bool
    target_date: date | None
    start_date: date | None
    end_date: date | None
    public_holidays: set[date]


def load_config(
    dry_run_cli: bool,
    *,
    debug_cli: bool,
    target_date_cli: str | None,
    start_date_cli: str | None,
    end_date_cli: str | None,
) -> Config:
    # Auto-load environment variables from .env in the current working directory.
    # Existing process variables are kept (override=False).
    load_dotenv(override=False)

    api_key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    workspace_id = os.environ.get("CLOCKIFY_WORKSPACE_ID", "").strip()
    project_id = (os.environ.get("CLOCKIFY_PROJECT_ID") or "").strip() or None
    tz_name = os.environ.get("TIMEZONE", "UTC").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        raise SystemExit(f"Invalid TIMEZONE {tz_name!r}: {e}") from e

    ws = os.environ.get("WORKDAY_START", "09:00").strip()
    we = os.environ.get("WORKDAY_END", "18:00").strip()
    h1, m1 = _parse_hhmm(ws)
    h2, m2 = _parse_hhmm(we)
    work_start = time(h1, m1)
    work_end = time(h2, m2)

    min_entries = int(os.environ.get("MIN_ENTRIES", "2"))
    max_entries = int(os.environ.get("MAX_ENTRIES", "4"))
    min_minutes = int(os.environ.get("MIN_MINUTES", "30"))
    max_minutes = int(os.environ.get("MAX_MINUTES", "120"))
    total_minutes = int(os.environ.get("TOTAL_MINUTES", "480"))
    step_minutes = int(os.environ.get("STEP_MINUTES", "5"))

    desc_raw = os.environ.get("DESCRIPTIONS")
    if desc_raw and desc_raw.strip():
        descriptions = [x.strip() for x in desc_raw.split(",") if x.strip()]
    else:
        descriptions = list(DEFAULT_DESCRIPTIONS)

    dry_run = dry_run_cli or _env_bool("DRY_RUN", False)
    debug = debug_cli or _env_bool("CLOCKIFY_DEBUG", False)
    target_date_raw = target_date_cli or os.environ.get("CLOCKIFY_TARGET_DATE", "").strip()
    start_date_raw = start_date_cli or os.environ.get("CLOCKIFY_START_DATE", "").strip()
    end_date_raw = end_date_cli or os.environ.get("CLOCKIFY_END_DATE", "").strip()

    def _parse_iso_day(value: str, label: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise SystemExit(f"Invalid {label} {value!r}; expected YYYY-MM-DD") from e

    target_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    if target_date_raw:
        target_date = _parse_iso_day(target_date_raw, "target date")
    if start_date_raw:
        start_date = _parse_iso_day(start_date_raw, "start date")
    if end_date_raw:
        end_date = _parse_iso_day(end_date_raw, "end date")
    if target_date is not None and (start_date is not None or end_date is not None):
        raise SystemExit("Use either TARGET_DATE or START_DATE/END_DATE, not both")
    if (start_date is None) != (end_date is None):
        raise SystemExit("Both START_DATE and END_DATE must be provided together")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise SystemExit("START_DATE must be <= END_DATE")

    holidays_raw = os.environ.get("PUBLIC_HOLIDAYS", "").strip()
    public_holidays: set[date] = set()
    if holidays_raw:
        for token in [x.strip() for x in holidays_raw.split(",") if x.strip()]:
            public_holidays.add(_parse_iso_day(token, "holiday"))

    if debug:
        if not api_key or not workspace_id:
            raise SystemExit(
                "CLOCKIFY_DEBUG requires CLOCKIFY_API_KEY and CLOCKIFY_WORKSPACE_ID"
            )

    if not dry_run and not debug and not api_key:
        raise SystemExit("CLOCKIFY_API_KEY is required unless DRY_RUN=1 or CLOCKIFY_DEBUG=1")
    if api_key and not workspace_id:
        raise SystemExit("CLOCKIFY_WORKSPACE_ID is required when CLOCKIFY_API_KEY is set")

    if min_entries < 1 or max_entries < min_entries:
        raise SystemExit("MIN_ENTRIES and MAX_ENTRIES must satisfy 1 <= MIN <= MAX")
    if min_minutes < 1 or max_minutes < min_minutes:
        raise SystemExit("MIN_MINUTES and MAX_MINUTES must satisfy 1 <= MIN <= MAX")
    if step_minutes < 1:
        raise SystemExit("STEP_MINUTES must be >= 1")
    if total_minutes < 1:
        raise SystemExit("TOTAL_MINUTES must be >= 1")
    if total_minutes % step_minutes != 0:
        raise SystemExit("TOTAL_MINUTES must be divisible by STEP_MINUTES")
    if min_minutes % step_minutes != 0 or max_minutes % step_minutes != 0:
        raise SystemExit("MIN_MINUTES and MAX_MINUTES must be divisible by STEP_MINUTES")

    return Config(
        api_key=api_key,
        workspace_id=workspace_id,
        project_id=project_id,
        tz=tz,
        work_start=work_start,
        work_end=work_end,
        min_entries=min_entries,
        max_entries=max_entries,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        total_minutes=total_minutes,
        step_minutes=step_minutes,
        descriptions=descriptions,
        ignore_day_has_entries=_env_bool("IGNORE_DAY_HAS_ENTRIES", False),
        dry_run=dry_run,
        debug=debug,
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        public_holidays=public_holidays,
    )


def _today_in_tz(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def _target_days(cfg: Config) -> list[date]:
    if cfg.start_date is not None and cfg.end_date is not None:
        days = (cfg.end_date - cfg.start_date).days
        return [cfg.start_date + timedelta(days=i) for i in range(days + 1)]
    if cfg.target_date is not None:
        return [cfg.target_date]
    return [_today_in_tz(cfg.tz)]


def _is_non_working_day(day: date, cfg: Config) -> bool:
    return day.weekday() >= 5 or day in cfg.public_holidays


def _window_bounds(day: date, cfg: Config) -> tuple[datetime, datetime]:
    start = datetime.combine(day, cfg.work_start, tzinfo=cfg.tz)
    end = datetime.combine(day, cfg.work_end, tzinfo=cfg.tz)
    if end <= start:
        raise SystemExit("WORKDAY_END must be after WORKDAY_START")
    return start, end


def _pick_feasible_entry_count(
    rng: random.Random,
    cfg: Config,
    window_slots: int,
) -> int:
    min_slots = cfg.min_minutes // cfg.step_minutes
    max_slots = cfg.max_minutes // cfg.step_minutes
    total_slots = cfg.total_minutes // cfg.step_minutes
    candidates: list[int] = []
    for n in range(cfg.min_entries, cfg.max_entries + 1):
        if n * min_slots <= total_slots <= n * max_slots and total_slots <= window_slots:
            candidates.append(n)
    if not candidates:
        raise RuntimeError(
            "No feasible entry count for TOTAL_MINUTES within current MIN/MAX settings and work window"
        )
    return rng.choice(candidates)


def _random_bounded_composition(
    rng: random.Random,
    total: int,
    parts: int,
    lower: int,
    upper: int,
) -> list[int]:
    if parts < 1:
        raise RuntimeError("parts must be >= 1")
    if parts * lower > total or parts * upper < total:
        raise RuntimeError("No bounded composition exists for requested values")
    values = [lower] * parts
    remaining = total - parts * lower
    while remaining > 0:
        idx = rng.randrange(parts)
        if values[idx] < upper:
            values[idx] += 1
            remaining -= 1
    rng.shuffle(values)
    return values


def _random_split_nonnegative(rng: random.Random, total: int, parts: int) -> list[int]:
    if parts < 1:
        raise RuntimeError("parts must be >= 1")
    values = [0] * parts
    for _ in range(total):
        values[rng.randrange(parts)] += 1
    return values


def _generate_intervals(
    rng: random.Random,
    window_start: datetime,
    window_end: datetime,
    cfg: Config,
) -> list[tuple[datetime, datetime]]:
    """Return non-overlapping intervals on STEP_MINUTES grid with exact TOTAL_MINUTES."""
    window_minutes = int((window_end - window_start).total_seconds() // 60)
    if window_minutes % cfg.step_minutes != 0:
        raise RuntimeError("Work window size must be divisible by STEP_MINUTES")
    window_slots = window_minutes // cfg.step_minutes

    total_slots = cfg.total_minutes // cfg.step_minutes
    min_slots = cfg.min_minutes // cfg.step_minutes
    max_slots = cfg.max_minutes // cfg.step_minutes
    n = _pick_feasible_entry_count(rng, cfg, window_slots)
    duration_slots = _random_bounded_composition(rng, total_slots, n, min_slots, max_slots)
    gap_slots = _random_split_nonnegative(rng, window_slots - total_slots, n + 1)

    out: list[tuple[datetime, datetime]] = []
    cursor = gap_slots[0]
    for i, dur in enumerate(duration_slots):
        start = window_start + timedelta(minutes=cursor * cfg.step_minutes)
        end = start + timedelta(minutes=dur * cfg.step_minutes)
        out.append((start, end))
        cursor += dur + gap_slots[i + 1]
    return out


def _make_rng() -> random.Random:
    raw = os.environ.get("CLOCKIFY_RANDOM_SEED", "").strip()
    if raw:
        return random.Random(int(raw))
    return random.Random()


def _build_planned_entries(
    cfg: Config,
    today: date,
) -> tuple[list[tuple[datetime, datetime]], list[str]]:
    rng = _make_rng()
    w_start, w_end = _window_bounds(today, cfg)
    intervals = _generate_intervals(
        rng,
        w_start,
        w_end,
        cfg,
    )
    descs = [rng.choice(cfg.descriptions) for _ in intervals]
    return intervals, descs


def run_debug(cfg: Config) -> int:
    print(
        "Clockify debug mode: no HTTP requests will be sent.\n",
        file=sys.stderr,
    )
    _debug_print_request("GET", f"{API_BASE}/user", api_key=cfg.api_key)
    post_url = f"{API_BASE}/workspaces/{cfg.workspace_id}/time-entries"

    for day in _target_days(cfg):
        if _is_non_working_day(day, cfg):
            print(f"Skip {day.isoformat()}: weekend/public holiday.", file=sys.stdout)
            continue

        day_start = datetime.combine(day, time(0, 0), tzinfo=cfg.tz)
        day_end = day_start + timedelta(days=1)
        start_utc = day_start.astimezone(timezone.utc)
        end_utc = day_end.astimezone(timezone.utc)

        print(f"=== Day {day.isoformat()} ===", file=sys.stdout)
        te_url = _time_entries_url(cfg.workspace_id, "<userId>", start_utc, end_utc)
        _debug_print_request("GET", te_url, api_key=cfg.api_key)

        if not cfg.ignore_day_has_entries:
            print(
                "  (If this response is a non-empty JSON array, the script exits and sends no POST requests for this day.)",
                file=sys.stdout,
            )
            print(file=sys.stdout)

        print("If the time-entries response is empty, the script would send:", file=sys.stdout)
        intervals, descs = _build_planned_entries(cfg, day)
        for (start, end), desc in zip(intervals, descs):
            body: dict[str, Any] = {
                "start": _utc_iso(start),
                "end": _utc_iso(end),
                "description": desc,
                "billable": False,
            }
            if cfg.project_id:
                body["projectId"] = cfg.project_id
            _debug_print_request("POST", post_url, api_key=cfg.api_key, body=body)

    return 0


def run(cfg: Config) -> int:
    if cfg.debug:
        return run_debug(cfg)

    if cfg.dry_run and not cfg.api_key:
        print("Dry-run: no API key — printing sample payloads only.", file=sys.stderr)

    uid: str | None = None
    if cfg.api_key:
        user = get_user(cfg.api_key)
        uid_raw = user.get("id")
        if not uid_raw:
            print("Could not read user id from /user", file=sys.stderr)
            return 1
        uid = str(uid_raw)

    for day in _target_days(cfg):
        if _is_non_working_day(day, cfg):
            print(f"Skip {day.isoformat()}: weekend/public holiday.", file=sys.stderr)
            continue

        if cfg.api_key and uid is not None and not cfg.ignore_day_has_entries:
            day_start = datetime.combine(day, time(0, 0), tzinfo=cfg.tz)
            day_end = day_start + timedelta(days=1)
            existing = list_time_entries(
                cfg.api_key,
                cfg.workspace_id,
                uid,
                day_start.astimezone(timezone.utc),
                day_end.astimezone(timezone.utc),
            )
            if existing:
                prefix = "Dry-run: skip — " if cfg.dry_run else "Skip: "
                print(
                    f"{prefix}{day.isoformat()} already has at least one time entry.",
                    file=sys.stderr,
                )
                continue

        intervals, descs = _build_planned_entries(cfg, day)
        for (start, end), desc in zip(intervals, descs):
            payload = {
                "start": _utc_iso(start),
                "end": _utc_iso(end),
                "description": desc,
                "projectId": cfg.project_id,
            }
            if cfg.dry_run:
                print(json.dumps(payload, indent=2))
            else:
                create_time_entry(
                    cfg.api_key,
                    cfg.workspace_id,
                    start=start,
                    end=end,
                    description=desc,
                    project_id=cfg.project_id,
                )
                print(f"Logged: {desc} {_utc_iso(start)} .. {_utc_iso(end)}")

    if cfg.dry_run:
        print("Dry-run: no entries created.", file=sys.stderr)

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    epilog = """
Environment variables:
  CLOCKIFY_API_KEY           API key (required for normal run and for CLOCKIFY_DEBUG; omit only for DRY_RUN without credentials)
  CLOCKIFY_WORKSPACE_ID      Workspace id (required whenever CLOCKIFY_API_KEY is set)
  CLOCKIFY_PROJECT_ID        Optional default project id for entries
  TIMEZONE                   IANA timezone for calendar day and work hours (default: UTC)
  WORKDAY_START, WORKDAY_END Local work window as HH:MM (default 09:00 / 18:00)
  MIN_ENTRIES, MAX_ENTRIES   Inclusive range of entries per run (default 2 / 4)
  MIN_MINUTES, MAX_MINUTES   Duration bounds per entry (default 30 / 120)
  TOTAL_MINUTES              Exact total logged minutes per day (default 480)
  STEP_MINUTES               Time granularity in minutes (default 5)
  DESCRIPTIONS               Comma-separated descriptions (overrides built-in list)
  IGNORE_DAY_HAS_ENTRIES     If 1, create entries even when today already has records (default 0)
  CLOCKIFY_DEBUG             If 1, print HTTP requests only; no network (default 0)
  CLOCKIFY_RANDOM_SEED       Optional int for reproducible random intervals (tests / debugging)
  CLOCKIFY_TARGET_DATE       Optional target date YYYY-MM-DD (defaults to current date in TIMEZONE)
  CLOCKIFY_START_DATE        Optional range start date YYYY-MM-DD (inclusive)
  CLOCKIFY_END_DATE          Optional range end date YYYY-MM-DD (inclusive)
  PUBLIC_HOLIDAYS            Optional comma-separated dates (YYYY-MM-DD) to skip
  DRY_RUN                    If 1, print payloads only (default 0)
"""
    p = argparse.ArgumentParser(
        description=(
            "Create random Clockify time entries for today in TIMEZONE "
            "(only if today has no existing entries, unless IGNORE_DAY_HAS_ENTRIES=1)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON payloads only; no POST. With API credentials, respects empty-day check.",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Print requests that would be sent (GET/POST); no network. Requires API key and workspace id.",
    )
    p.add_argument(
        "--date",
        help="Target calendar date in YYYY-MM-DD. If omitted, uses current date in TIMEZONE.",
    )
    p.add_argument(
        "--start-date",
        help="Start date YYYY-MM-DD (inclusive). Must be used with --end-date.",
    )
    p.add_argument(
        "--end-date",
        help="End date YYYY-MM-DD (inclusive). Must be used with --start-date.",
    )
    return p


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    cfg = load_config(
        dry_run_cli=args.dry_run,
        debug_cli=args.debug,
        target_date_cli=args.date,
        start_date_cli=args.start_date,
        end_date_cli=args.end_date,
    )
    try:
        code = run(cfg)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    raise SystemExit(code)


if __name__ == "__main__":
    main()
