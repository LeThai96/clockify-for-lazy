#!/usr/bin/env python3
"""
Add tags to existing Clockify time entries based on description mapping.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from clockify_tags import DESCRIPTION_TO_TAG_NAME, find_missing_tag_names, resolve_tag_id, tags_by_name

API_BASE = "https://api.clockify.me/api/v1"

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _today_in_tz(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def _target_days(cfg: "Config") -> list[date]:
    if cfg.start_date is not None and cfg.end_date is not None:
        days = (cfg.end_date - cfg.start_date).days
        return [cfg.start_date + timedelta(days=i) for i in range(days + 1)]
    if cfg.target_date is not None:
        return [cfg.target_date]
    return [_today_in_tz(cfg.tz)]


def get_user(api_key: str) -> dict[str, Any]:
    out = _http_json("GET", f"{API_BASE}/user", api_key=api_key)
    if not isinstance(out, dict):
        raise RuntimeError("Unexpected /user response")
    return out


def list_workspace_tags(api_key: str, workspace_id: str) -> list[dict[str, Any]]:
    out = _http_json("GET", f"{API_BASE}/workspaces/{workspace_id}/tags", api_key=api_key)
    if not isinstance(out, list):
        raise RuntimeError("Unexpected /tags response")
    return out


def list_time_entries(
    api_key: str,
    workspace_id: str,
    user_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    qs = "&".join(
        [
            f"start={urllib.parse.quote(_utc_iso(start_utc))}",
            f"end={urllib.parse.quote(_utc_iso(end_utc))}",
            "page-size=5000",
        ]
    )
    url = f"{API_BASE}/workspaces/{workspace_id}/user/{user_id}/time-entries?{qs}"
    out = _http_json("GET", url, api_key=api_key)
    if not isinstance(out, list):
        raise RuntimeError("Unexpected time entries response")
    return out


def update_time_entry_tags(
    api_key: str,
    workspace_id: str,
    entry_id: str,
    tag_ids: list[str],
    *,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}/workspaces/{workspace_id}/time-entries/{entry_id}"
    try:
        out = _http_json("PUT", url, api_key=api_key, body={"tagIds": tag_ids})
    except RuntimeError as e:
        if "HTTP 400" not in str(e) or entry is None:
            raise
        time_interval = entry.get("timeInterval")
        if not isinstance(time_interval, dict):
            raise
        start = time_interval.get("start")
        if not isinstance(start, str):
            raise
        body: dict[str, Any] = {
            "start": start,
            "billable": bool(entry.get("billable", False)),
            "tagIds": tag_ids,
            "description": str(entry.get("description") or ""),
        }
        end = time_interval.get("end")
        if isinstance(end, str):
            body["end"] = end
        project_id = entry.get("projectId")
        if isinstance(project_id, str) and project_id:
            body["projectId"] = project_id
        task_id = entry.get("taskId")
        if isinstance(task_id, str) and task_id:
            body["taskId"] = task_id
        out = _http_json("PUT", url, api_key=api_key, body=body)
    if isinstance(out, dict):
        return out
    raise RuntimeError("Unexpected update time entry response")


def _parse_iso_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise SystemExit(f"Invalid {label} {value!r}; expected YYYY-MM-DD") from e


@dataclass
class Config:
    api_key: str
    workspace_id: str
    tz: ZoneInfo
    target_date: date | None
    start_date: date | None
    end_date: date | None
    dry_run: bool


def load_config(
    *,
    dry_run_cli: bool,
    target_date_cli: str | None,
    start_date_cli: str | None,
    end_date_cli: str | None,
) -> Config:
    load_dotenv(override=False)
    api_key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    workspace_id = os.environ.get("CLOCKIFY_WORKSPACE_ID", "").strip()
    tz_name = os.environ.get("TIMEZONE", "UTC").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        raise SystemExit(f"Invalid TIMEZONE {tz_name!r}: {e}") from e

    target_date_raw = target_date_cli or os.environ.get("CLOCKIFY_TARGET_DATE", "").strip()
    start_date_raw = start_date_cli or os.environ.get("CLOCKIFY_START_DATE", "").strip()
    end_date_raw = end_date_cli or os.environ.get("CLOCKIFY_END_DATE", "").strip()
    dry_run = dry_run_cli or _env_bool("DRY_RUN", False)

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

    if not dry_run and not api_key:
        raise SystemExit("CLOCKIFY_API_KEY is required unless DRY_RUN=1")
    if api_key and not workspace_id:
        raise SystemExit("CLOCKIFY_WORKSPACE_ID is required when CLOCKIFY_API_KEY is set")

    return Config(
        api_key=api_key,
        workspace_id=workspace_id,
        tz=tz,
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        dry_run=dry_run,
    )


def run(cfg: Config) -> int:
    if cfg.dry_run and not cfg.api_key:
        print("Dry-run: no API key — no network calls will be made.", file=sys.stderr)
        for day in _target_days(cfg):
            print(f"Would process {day.isoformat()} for mapped descriptions.")
        return 0

    user = get_user(cfg.api_key)
    user_id = str(user.get("id") or "")
    if not user_id:
        print("Could not read user id from /user", file=sys.stderr)
        return 1

    tags = list_workspace_tags(cfg.api_key, cfg.workspace_id)
    tag_name_to_id = tags_by_name(tags)

    missing_tag_names = find_missing_tag_names(tag_name_to_id)
    if missing_tag_names:
        print(
            f"Missing workspace tags: {', '.join(missing_tag_names)}",
            file=sys.stderr,
        )
        return 1

    updated_count = 0
    skipped_count = 0

    for day in _target_days(cfg):
        day_start = datetime.combine(day, time(0, 0), tzinfo=cfg.tz)
        day_end = day_start + timedelta(days=1)
        entries = list_time_entries(
            cfg.api_key,
            cfg.workspace_id,
            user_id,
            day_start.astimezone(timezone.utc),
            day_end.astimezone(timezone.utc),
        )
        for entry in entries:
            description = entry.get("description")
            entry_id = entry.get("id")
            if not isinstance(description, str) or not isinstance(entry_id, str):
                skipped_count += 1
                continue
            mapped_tag_name = DESCRIPTION_TO_TAG_NAME.get(description)
            if mapped_tag_name is None:
                skipped_count += 1
                continue

            mapped_tag_id = resolve_tag_id(tag_name_to_id, mapped_tag_name)
            if mapped_tag_id is None:
                skipped_count += 1
                continue
            existing_tag_ids = entry.get("tagIds")
            if not isinstance(existing_tag_ids, list):
                existing_tag_ids = []
            clean_tag_ids = [t for t in existing_tag_ids if isinstance(t, str)]
            if mapped_tag_id in clean_tag_ids:
                skipped_count += 1
                continue
            new_tag_ids = clean_tag_ids + [mapped_tag_id]

            if cfg.dry_run:
                print(
                    f"Dry-run: would tag entry {entry_id} "
                    f"({description}) with {mapped_tag_name}"
                )
            else:
                update_time_entry_tags(
                    cfg.api_key,
                    cfg.workspace_id,
                    entry_id,
                    new_tag_ids,
                    entry=entry,
                )
                print(
                    f"Tagged entry {entry_id} "
                    f"({description}) with {mapped_tag_name}"
                )
            updated_count += 1

    print(f"Done. updated={updated_count}, skipped={skipped_count}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    epilog = """
Environment variables:
  CLOCKIFY_API_KEY           API key (required unless DRY_RUN=1)
  CLOCKIFY_WORKSPACE_ID      Workspace id (required when API key is set)
  TIMEZONE                   IANA timezone for selecting date windows (default: UTC)
  CLOCKIFY_TARGET_DATE       Optional target date YYYY-MM-DD (defaults to current date in TIMEZONE)
  CLOCKIFY_START_DATE        Optional range start date YYYY-MM-DD (inclusive)
  CLOCKIFY_END_DATE          Optional range end date YYYY-MM-DD (inclusive)
  DRY_RUN                    If 1, print updates only (default 0)
"""
    p = argparse.ArgumentParser(
        description="Add tags to existing Clockify entries by description mapping.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument("--dry-run", action="store_true", help="Preview updates without PUT.")
    p.add_argument("--date", help="Target date YYYY-MM-DD.")
    p.add_argument("--start-date", help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end-date", help="End date YYYY-MM-DD (inclusive).")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    cfg = load_config(
        dry_run_cli=args.dry_run,
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
