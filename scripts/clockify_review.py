#!/usr/bin/env python3
"""
Review a month of Clockify time entries against env schedule rules.
Each run is saved under a GUID; --overwrite GUID replaces incorrect days
with the replacements stored in that run.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

import clockify_daily as cd
from clockify_tags import DESCRIPTION_TO_TAG_NAME, find_missing_tag_names, resolve_tag_id, tags_by_name

REVIEWS_DIR = Path("reviews")
PAGE_SIZE = 5000


@dataclass
class ReviewConfig:
    api_key: str
    workspace_id: str
    project_id: str | None
    tz: ZoneInfo
    tz_name: str
    work_start: time
    work_end: time
    min_entries: int
    max_entries: int
    min_minutes: int
    max_minutes: int
    total_minutes: int
    step_minutes: int
    descriptions: list[str]
    public_holidays: set[date]
    day_offs: set[date]
    dry_run: bool
    year: int
    month: int
    overwrite_guid: str | None


@dataclass
class ParsedEntry:
    id: str | None
    start: datetime | None
    end: datetime | None
    description: str
    billable: bool
    project_id: str | None
    tag_ids: list[str]


@dataclass
class PlannedEntry:
    start: datetime
    end: datetime
    description: str
    billable: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "start": cd._utc_iso(self.start),
            "end": cd._utc_iso(self.end),
            "description": self.description,
            "billable": self.billable,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PlannedEntry:
        return cls(
            start=_parse_clockify_dt(str(raw["start"])),
            end=_parse_clockify_dt(str(raw["end"])),
            description=str(raw["description"]),
            billable=bool(raw["billable"]),
        )


@dataclass
class DayResult:
    day: date
    kind: str
    issues: list[str]
    entry_ids: list[str]
    replacements: list[PlannedEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass
class UnscopedEntry:
    entry_id: str | None
    issues: list[str]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_month(value: str) -> tuple[int, int]:
    parts = value.strip().split("-")
    if len(parts) != 2:
        raise SystemExit(f"Invalid month {value!r}; expected YYYY-MM")
    try:
        year = int(parts[0])
        month = int(parts[1])
        date(year, month, 1)
    except ValueError as e:
        raise SystemExit(f"Invalid month {value!r}; expected YYYY-MM") from e
    if month < 1 or month > 12:
        raise SystemExit(f"Invalid month {value!r}; expected YYYY-MM")
    return year, month


def _parse_guid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise SystemExit(f"Invalid guid {value!r}") from e


def _new_guid() -> str:
    return str(uuid.uuid4())


def _today_in_tz(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def _month_last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _reviewed_days(year: int, month: int, today: date) -> list[date]:
    first = date(year, month, 1)
    last = _month_last_day(year, month)
    if today < first:
        return []
    end = min(last, today)
    return [first + timedelta(days=i) for i in range((end - first).days + 1)]


def _parse_clockify_dt(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def _same_instant(left: datetime, right: datetime) -> bool:
    return left.astimezone(timezone.utc).replace(microsecond=0) == right.astimezone(
        timezone.utc
    ).replace(microsecond=0)


def _entry_tag_ids(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("tagIds")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for tag in tags:
        if isinstance(tag, dict) and isinstance(tag.get("id"), str):
            out.append(tag["id"])
        elif isinstance(tag, str):
            out.append(tag)
    return out


def _parse_entry(entry: dict[str, Any], tz: ZoneInfo) -> ParsedEntry:
    interval = entry.get("timeInterval")
    start: datetime | None = None
    end: datetime | None = None
    if isinstance(interval, dict):
        start_raw = interval.get("start")
        end_raw = interval.get("end")
        if isinstance(start_raw, str) and start_raw:
            start = _parse_clockify_dt(start_raw).astimezone(tz)
        if isinstance(end_raw, str) and end_raw:
            end = _parse_clockify_dt(end_raw).astimezone(tz)
    project_raw = entry.get("projectId")
    project_id = project_raw if isinstance(project_raw, str) and project_raw else None
    desc_raw = entry.get("description")
    description = desc_raw if isinstance(desc_raw, str) else ""
    return ParsedEntry(
        id=cd._entry_id(entry),
        start=start,
        end=end,
        description=description,
        billable=bool(entry.get("billable", False)),
        project_id=project_id,
        tag_ids=_entry_tag_ids(entry),
    )


def _label(entry: ParsedEntry, index: int) -> str:
    if entry.id:
        return entry.id
    return f"entry {index + 1}"


def _on_step_grid(dt: datetime, window_start: datetime, step: int) -> bool:
    offset = int((dt - window_start).total_seconds() // 60)
    return offset % step == 0


def _check_project(cfg: ReviewConfig, entry: ParsedEntry, label: str) -> list[str]:
    if cfg.project_id and entry.project_id != cfg.project_id:
        return [f"{label}: project should be {cfg.project_id}"]
    return []


def _check_tag(
    entry: ParsedEntry,
    label: str,
    expected_tag_name: str,
    tag_name_to_id: dict[str, str],
) -> list[str]:
    expected_id = resolve_tag_id(tag_name_to_id, expected_tag_name)
    if expected_id is None or entry.tag_ids != [expected_id]:
        return [f"{label}: tag should be {expected_tag_name}"]
    return []


def _check_special_shape(
    cfg: ReviewConfig,
    entry: ParsedEntry,
    index: int,
    *,
    expected_start: datetime,
    expected_end: datetime,
    expected_description: str,
    expected_tag_name: str,
    tag_name_to_id: dict[str, str],
) -> list[str]:
    label = _label(entry, index)
    issues: list[str] = []
    if entry.description != expected_description:
        issues.append(f"{label}: description should be {expected_description!r}")
    if entry.billable:
        issues.append(f"{label}: billable should be false")
    if entry.start is None:
        issues.append(f"{label}: missing start")
    elif not _same_instant(entry.start, expected_start):
        issues.append(f"{label}: start should be {expected_start.isoformat()}")
    if entry.end is None:
        issues.append(f"{label}: missing end")
    elif not _same_instant(entry.end, expected_end):
        issues.append(f"{label}: end should be {expected_end.isoformat()}")
    issues.extend(_check_tag(entry, label, expected_tag_name, tag_name_to_id))
    issues.extend(_check_project(cfg, entry, label))
    return issues


def _planned_from_tuple(
    item: tuple[datetime, datetime, str, bool],
) -> PlannedEntry:
    start, end, description, billable = item
    return PlannedEntry(start=start, end=end, description=description, billable=billable)


def _workday_replacements(cfg: ReviewConfig, day: date, run_id: str) -> list[PlannedEntry]:
    rng = random.Random(f"{run_id}:{day.isoformat()}")
    window_start, window_end = cd._window_bounds(day, cfg)
    intervals = cd._generate_intervals(rng, window_start, window_end, cfg)
    descriptions = [rng.choice(cfg.descriptions) for _ in intervals]
    return [
        PlannedEntry(start=start, end=end, description=desc, billable=True)
        for (start, end), desc in zip(intervals, descriptions)
    ]


def _review_day(
    cfg: ReviewConfig,
    day: date,
    entries: list[ParsedEntry],
    tag_name_to_id: dict[str, str],
    *,
    run_id: str,
) -> DayResult:
    kind = cd._day_kind(day, cfg)
    ids = [entry.id for entry in entries if entry.id]
    if kind == "weekend":
        issues: list[str] = []
        if entries:
            issues.append(f"weekend should have no entries, found {len(entries)}")
        return DayResult(day=day, kind=kind, issues=issues, entry_ids=ids, replacements=[])

    if kind == "day_off":
        expected = _planned_from_tuple(cd._build_day_off_entry(cfg, day))
        issues = []
        if len(entries) != 1:
            issues.append(f"expected exactly one Annual leave entry, found {len(entries)}")
        for index, entry in enumerate(entries):
            issues.extend(
                _check_special_shape(
                    cfg,
                    entry,
                    index,
                    expected_start=expected.start,
                    expected_end=expected.end,
                    expected_description="Annual leave",
                    expected_tag_name="On-Leave",
                    tag_name_to_id=tag_name_to_id,
                )
            )
        replacements = [] if not issues else [expected]
        return DayResult(
            day=day, kind=kind, issues=issues, entry_ids=ids, replacements=replacements
        )

    if kind == "public_holiday":
        expected = _planned_from_tuple(
            cd._build_special_day_entry(cfg, day, description="Public holiday")
        )
        issues = []
        if len(entries) != 1:
            issues.append(f"expected exactly one Public holiday entry, found {len(entries)}")
        for index, entry in enumerate(entries):
            issues.extend(
                _check_special_shape(
                    cfg,
                    entry,
                    index,
                    expected_start=expected.start,
                    expected_end=expected.end,
                    expected_description="Public holiday",
                    expected_tag_name="Public Holiday",
                    tag_name_to_id=tag_name_to_id,
                )
            )
        replacements = [] if not issues else [expected]
        return DayResult(
            day=day, kind=kind, issues=issues, entry_ids=ids, replacements=replacements
        )

    issues = _review_workday(cfg, day, entries, tag_name_to_id)
    replacements = [] if not issues else _workday_replacements(cfg, day, run_id)
    return DayResult(
        day=day, kind=kind, issues=issues, entry_ids=ids, replacements=replacements
    )


def _review_workday(
    cfg: ReviewConfig,
    day: date,
    entries: list[ParsedEntry],
    tag_name_to_id: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    if not (cfg.min_entries <= len(entries) <= cfg.max_entries):
        issues.append(
            f"entry count {len(entries)} is outside {cfg.min_entries}-{cfg.max_entries}"
        )

    complete: list[tuple[int, ParsedEntry]] = []
    total = 0
    window_start, window_end = cd._window_bounds(day, cfg)

    for index, entry in enumerate(entries):
        label = _label(entry, index)
        if entry.start is None:
            issues.append(f"{label}: missing start")
        if entry.end is None:
            issues.append(f"{label}: missing end")
        if entry.start is None or entry.end is None:
            continue
        minutes = int((entry.end - entry.start).total_seconds() // 60)
        total += minutes
        complete.append((index, entry))
        if not (cfg.min_minutes <= minutes <= cfg.max_minutes):
            issues.append(
                f"{label}: duration {minutes} is outside {cfg.min_minutes}-{cfg.max_minutes}"
            )
        if minutes % cfg.step_minutes != 0:
            issues.append(
                f"{label}: duration is not on the {cfg.step_minutes}-minute grid"
            )
        if not _on_step_grid(entry.start, window_start, cfg.step_minutes):
            issues.append(
                f"{label}: start is not on the {cfg.step_minutes}-minute grid from WORKDAY_START"
            )
        if not _on_step_grid(entry.end, window_start, cfg.step_minutes):
            issues.append(
                f"{label}: end is not on the {cfg.step_minutes}-minute grid from WORKDAY_START"
            )
        if entry.start < window_start or entry.end > window_end:
            issues.append(f"{label}: outside work window")
        if not entry.billable:
            issues.append(f"{label}: not billable")
        if entry.description not in cfg.descriptions:
            issues.append(
                f"{label}: description {entry.description!r} is not in DESCRIPTIONS"
            )
        tag_name = DESCRIPTION_TO_TAG_NAME.get(entry.description)
        if tag_name is None:
            issues.append(f"{label}: no tag mapping for description {entry.description!r}")
        else:
            issues.extend(_check_tag(entry, label, tag_name, tag_name_to_id))
        issues.extend(_check_project(cfg, entry, label))

    if total != cfg.total_minutes:
        issues.append(f"total minutes {total} != {cfg.total_minutes}")

    ordered = sorted(complete, key=lambda item: item[1].start or datetime.min.replace(tzinfo=cfg.tz))
    for (_i, left), (_j, right) in zip(ordered, ordered[1:]):
        if left.end is not None and right.start is not None and left.end > right.start:
            issues.append("entries overlap")
            break
    return issues


def load_config(
    *,
    dry_run_cli: bool,
    month_cli: str | None,
    overwrite_cli: str | None,
) -> ReviewConfig:
    load_dotenv(override=False)

    if overwrite_cli and month_cli:
        raise SystemExit("Use either --month or --overwrite, not both")

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
    h1, m1 = cd._parse_hhmm(ws)
    h2, m2 = cd._parse_hhmm(we)
    work_start = time(h1, m1)
    work_end = time(h2, m2)
    if work_end <= work_start:
        raise SystemExit("WORKDAY_END must be after WORKDAY_START")

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
        descriptions = list(cd.DEFAULT_DESCRIPTIONS)

    public_holidays = cd._parse_date_tokens(os.environ.get("PUBLIC_HOLIDAYS", ""), "holiday")
    day_offs = cd._parse_date_tokens(os.environ.get("DAY_OFF", ""), "day off")

    if not api_key:
        raise SystemExit("CLOCKIFY_API_KEY is required")
    if not workspace_id:
        raise SystemExit("CLOCKIFY_WORKSPACE_ID is required")

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

    overwrite_guid = _parse_guid(overwrite_cli) if overwrite_cli else None
    today = _today_in_tz(tz)
    if month_cli:
        year, month = _parse_month(month_cli)
    else:
        year, month = today.year, today.month

    return ReviewConfig(
        api_key=api_key,
        workspace_id=workspace_id,
        project_id=project_id,
        tz=tz,
        tz_name=tz_name,
        work_start=work_start,
        work_end=work_end,
        min_entries=min_entries,
        max_entries=max_entries,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        total_minutes=total_minutes,
        step_minutes=step_minutes,
        descriptions=descriptions,
        public_holidays=public_holidays,
        day_offs=day_offs,
        dry_run=dry_run_cli or _env_bool("DRY_RUN", False),
        year=year,
        month=month,
        overwrite_guid=overwrite_guid,
    )


def _workspace_context(cfg: ReviewConfig) -> tuple[str, dict[str, str]]:
    user = cd.get_user(cfg.api_key)
    uid_raw = user.get("id")
    if not uid_raw:
        raise RuntimeError("Could not read user id from /user")
    tags = cd.list_workspace_tags(cfg.api_key, cfg.workspace_id)
    tag_name_to_id = tags_by_name(tags)
    missing = find_missing_tag_names(tag_name_to_id)
    if missing:
        raise SystemExit(f"Missing workspace tags: {', '.join(missing)}")
    return str(uid_raw), tag_name_to_id


def _bucket_entries(
    raw_entries: list[dict[str, Any]],
    reviewed: set[date],
    tz: ZoneInfo,
) -> tuple[dict[date, list[ParsedEntry]], list[UnscopedEntry]]:
    by_day: dict[date, list[ParsedEntry]] = {day: [] for day in reviewed}
    unscoped: list[UnscopedEntry] = []
    for raw in raw_entries:
        parsed = _parse_entry(raw, tz)
        if parsed.start is None:
            unscoped.append(
                UnscopedEntry(entry_id=parsed.id, issues=[f"{parsed.id or 'entry'}: missing start"])
            )
            continue
        local_day = parsed.start.date()
        if local_day not in reviewed:
            unscoped.append(
                UnscopedEntry(
                    entry_id=parsed.id,
                    issues=[
                        f"{parsed.id or 'entry'}: start {parsed.start.isoformat()} is outside reviewed days"
                    ],
                )
            )
            continue
        by_day[local_day].append(parsed)
    return by_day, unscoped


def _run_path(run_id: str) -> Path:
    return REVIEWS_DIR / f"{run_id}.json"


def _day_result_json(result: DayResult) -> dict[str, Any]:
    return {
        "date": result.day.isoformat(),
        "kind": result.kind,
        "issues": result.issues,
        "entry_ids": result.entry_ids,
        "replacements": [item.to_json() for item in result.replacements],
    }


def _print_report(
    run_id: str,
    cfg: ReviewConfig,
    today: date,
    results: list[DayResult],
    unscoped: list[UnscopedEntry],
    path: Path,
) -> None:
    month_label = f"{cfg.year:04d}-{cfg.month:02d}"
    ok_count = sum(1 for item in results if item.ok)
    bad = [item for item in results if not item.ok]
    print(f"Review run: {run_id}")
    print(f"Month: {month_label} ({cfg.tz_name}), through {today.isoformat()}")
    print(f"OK: {ok_count} days")
    print(f"Incorrect: {len(bad)} days")
    if unscoped:
        print(f"Unscoped entries: {len(unscoped)}")
        for item in unscoped:
            for issue in item.issues:
                print(f"  - {issue}")
    for item in bad:
        print()
        print(f"{item.day.isoformat()} ({item.kind}):")
        for issue in item.issues:
            print(f"  - {issue}")
    print()
    print(f"Saved: {path.as_posix()}")


def run_review(cfg: ReviewConfig) -> int:
    today = _today_in_tz(cfg.tz)
    reviewed = _reviewed_days(cfg.year, cfg.month, today)
    uid, tag_name_to_id = _workspace_context(cfg)
    raw_entries: list[dict[str, Any]] = []
    if reviewed:
        start = datetime.combine(reviewed[0], time(0, 0), tzinfo=cfg.tz)
        end = datetime.combine(reviewed[-1] + timedelta(days=1), time(0, 0), tzinfo=cfg.tz)
        raw_entries = cd.list_time_entries(
            cfg.api_key,
            cfg.workspace_id,
            uid,
            start.astimezone(timezone.utc),
            end.astimezone(timezone.utc),
            page_size=PAGE_SIZE,
        )
    by_day, unscoped = _bucket_entries(raw_entries, set(reviewed), cfg.tz)
    run_id = _new_guid()
    results = [
        _review_day(cfg, day, by_day.get(day, []), tag_name_to_id, run_id=run_id)
        for day in reviewed
    ]
    payload = {
        "guid": run_id,
        "month": f"{cfg.year:04d}-{cfg.month:02d}",
        "timezone": cfg.tz_name,
        "reviewed_through": (reviewed[-1].isoformat() if reviewed else None),
        "ok_days": sum(1 for item in results if item.ok),
        "incorrect_days": [_day_result_json(item) for item in results if not item.ok],
        "unscoped": [
            {"id": item.entry_id, "issues": item.issues} for item in unscoped
        ],
    }
    path = _run_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _print_report(run_id, cfg, reviewed[-1] if reviewed else today, results, unscoped, path)
    return 0


def _load_run(run_id: str) -> dict[str, Any]:
    path = _run_path(run_id)
    if not path.is_file():
        raise SystemExit(f"Review run not found: {path.as_posix()}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid review run file: {path.as_posix()}") from e
    if not isinstance(raw, dict):
        raise SystemExit(f"Invalid review run file: {path.as_posix()}")
    return raw


def _create_planned(
    cfg: ReviewConfig,
    planned: PlannedEntry,
    tag_name_to_id: dict[str, str],
) -> None:
    tag_name = DESCRIPTION_TO_TAG_NAME.get(planned.description)
    tag_id = (
        resolve_tag_id(tag_name_to_id, tag_name)
        if tag_name is not None and tag_name_to_id
        else None
    )
    tag_ids = [tag_id] if tag_id is not None else []
    cd.create_time_entry(
        cfg.api_key,
        cfg.workspace_id,
        start=planned.start,
        end=planned.end,
        description=planned.description,
        project_id=cfg.project_id,
        billable=planned.billable,
        tag_ids=tag_ids,
    )


def apply_review(cfg: ReviewConfig) -> int:
    assert cfg.overwrite_guid is not None
    run = _load_run(cfg.overwrite_guid)
    uid, tag_name_to_id = _workspace_context(cfg)
    incorrect = run.get("incorrect_days")
    if not isinstance(incorrect, list):
        raise SystemExit(f"Invalid review run file: {_run_path(cfg.overwrite_guid).as_posix()}")

    print(f"Overwrite run: {cfg.overwrite_guid}")
    for item in incorrect:
        if not isinstance(item, dict):
            continue
        day_raw = item.get("date")
        if not isinstance(day_raw, str):
            continue
        day = date.fromisoformat(day_raw)
        kind = str(item.get("kind") or cd._day_kind(day, cfg))
        replacements_raw = item.get("replacements")
        replacements = [
            PlannedEntry.from_json(raw)
            for raw in replacements_raw
            if isinstance(raw, dict)
        ] if isinstance(replacements_raw, list) else []

        day_start = datetime.combine(day, time(0, 0), tzinfo=cfg.tz)
        day_end = day_start + timedelta(days=1)
        existing = cd.list_time_entries(
            cfg.api_key,
            cfg.workspace_id,
            uid,
            day_start.astimezone(timezone.utc),
            day_end.astimezone(timezone.utc),
            page_size=PAGE_SIZE,
        )
        ids = [eid for eid in (cd._entry_id(entry) for entry in existing) if eid is not None]
        joined = ", ".join(ids) if ids else "(none)"
        if cfg.dry_run:
            print(f"{day.isoformat()} ({kind}): would delete {len(ids)} entries: {joined}")
            for planned in replacements:
                print(
                    f"  would create: {planned.description} "
                    f"{cd._utc_iso(planned.start)} .. {cd._utc_iso(planned.end)}"
                )
            continue
        for eid in ids:
            cd.delete_time_entry(cfg.api_key, cfg.workspace_id, eid)
            print(f"Deleted: {eid}", file=sys.stderr)
        for planned in replacements:
            _create_planned(cfg, planned, tag_name_to_id)
            print(
                f"Logged: {planned.description} "
                f"{cd._utc_iso(planned.start)} .. {cd._utc_iso(planned.end)}"
            )
    if cfg.dry_run:
        print("Dry-run: no entries changed.", file=sys.stderr)
    return 0


def run(cfg: ReviewConfig) -> int:
    if cfg.overwrite_guid:
        return apply_review(cfg)
    return run_review(cfg)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Review a month of Clockify time entries against env schedule rules. "
            "Default month is the current month in TIMEZONE. "
            "Each run is saved under a GUID; pass that GUID to --overwrite to replace incorrect days."
        )
    )
    p.add_argument(
        "--month",
        help="Month to review as YYYY-MM. Default: current month in TIMEZONE.",
    )
    p.add_argument(
        "--overwrite",
        metavar="GUID",
        help="Apply replacements stored in a previous review run. Does not re-review.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="With --overwrite, print deletes and creates without sending them.",
    )
    return p


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    cfg = load_config(
        dry_run_cli=args.dry_run,
        month_cli=args.month,
        overwrite_cli=args.overwrite,
    )
    try:
        code = run(cfg)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    raise SystemExit(code)


if __name__ == "__main__":
    main()
