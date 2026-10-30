"""Tests for clockify_review (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

import clockify_daily as cd
import clockify_review as cr
from clockify_tags import tags_by_name

UTC = ZoneInfo("UTC")
GUID = "11111111-1111-1111-1111-111111111111"

WORKSPACE_TAGS = [
    {"id": "tag-meeting", "name": "Meeting"},
    {"id": "tag-dev", "name": "Development"},
    {"id": "tag-bug", "name": "Bug fixes"},
    {"id": "tag-review", "name": "Code review"},
    {"id": "tag-doc", "name": "Documenting"},
    {"id": "tag-leave", "name": "On-Leave"},
    {"id": "tag-holiday", "name": "Public Holiday"},
]
TAG_MAP = tags_by_name(WORKSPACE_TAGS)


def _cfg(**overrides: object) -> cr.ReviewConfig:
    values: dict[str, object] = {
        "api_key": "abcdefghijklmnop",
        "workspace_id": "ws",
        "project_id": None,
        "tz": UTC,
        "tz_name": "UTC",
        "work_start": time(9, 0),
        "work_end": time(18, 0),
        "min_entries": 2,
        "max_entries": 2,
        "min_minutes": 60,
        "max_minutes": 120,
        "total_minutes": 180,
        "step_minutes": 5,
        "descriptions": list(cd.DEFAULT_DESCRIPTIONS),
        "public_holidays": set(),
        "day_offs": set(),
        "dry_run": False,
        "year": 2026,
        "month": 4,
        "overwrite_guid": None,
    }
    values.update(overrides)
    return cr.ReviewConfig(**values)  # type: ignore[arg-type]


def _dt(hour: int, minute: int = 0, day: date = date(2026, 4, 1)) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _pe(
    entry_id: str,
    start: datetime | None,
    end: datetime | None,
    description: str,
    *,
    billable: bool = True,
    tag_id: str | None = "tag-meeting",
    project_id: str | None = None,
) -> cr.ParsedEntry:
    return cr.ParsedEntry(
        id=entry_id,
        start=start,
        end=end,
        description=description,
        billable=billable,
        project_id=project_id,
        tag_ids=[tag_id] if tag_id else [],
    )


def _valid_workday_pair(day: date = date(2026, 4, 1)) -> list[cr.ParsedEntry]:
    return [
        _pe("e1", _dt(9, day=day), _dt(11, day=day), "Meeting", tag_id="tag-meeting"),
        _pe(
            "e2",
            _dt(13, day=day),
            _dt(14, day=day),
            "Doing tasks",
            tag_id="tag-dev",
        ),
    ]


def _raw(
    entry_id: str,
    start: datetime,
    end: datetime,
    description: str,
    *,
    billable: bool = True,
    tag_ids: list[str] | None = None,
    project_id: str | None = None,
) -> dict:
    body = {
        "id": entry_id,
        "description": description,
        "billable": billable,
        "tagIds": tag_ids or [],
        "timeInterval": {
            "start": cd._utc_iso(start),
            "end": cd._utc_iso(end),
        },
    }
    if project_id:
        body["projectId"] = project_id
    return body


def _env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("MIN_ENTRIES", "2")
    monkeypatch.setenv("MAX_ENTRIES", "2")
    monkeypatch.setenv("MIN_MINUTES", "60")
    monkeypatch.setenv("MAX_MINUTES", "120")
    monkeypatch.setenv("TOTAL_MINUTES", "180")
    monkeypatch.setenv("STEP_MINUTES", "5")
    monkeypatch.delenv("CLOCKIFY_PROJECT_ID", raising=False)
    monkeypatch.delenv("DAY_OFF", raising=False)
    monkeypatch.delenv("PUBLIC_HOLIDAYS", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)


def _stub_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    existing: list[dict] | None = None,
    guid: str = GUID,
) -> None:
    monkeypatch.setattr(cr, "REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(cr, "_new_guid", lambda: guid)
    monkeypatch.setattr(cr.cd, "get_user", lambda _k: {"id": "u1"})
    monkeypatch.setattr(cr.cd, "list_workspace_tags", lambda *_a, **_k: WORKSPACE_TAGS)
    monkeypatch.setattr(cr.cd, "list_time_entries", lambda *a, **k: list(existing or []))


def test_reviewed_days_current_month_through_today() -> None:
    days = cr._reviewed_days(2026, 4, date(2026, 4, 15))
    assert days[0] == date(2026, 4, 1)
    assert days[-1] == date(2026, 4, 15)
    assert len(days) == 15


def test_reviewed_days_past_month_is_full() -> None:
    days = cr._reviewed_days(2026, 3, date(2026, 4, 15))
    assert days[0] == date(2026, 3, 1)
    assert days[-1] == date(2026, 3, 31)
    assert len(days) == 31


def test_reviewed_days_future_month_is_empty() -> None:
    assert cr._reviewed_days(2026, 5, date(2026, 4, 15)) == []


def test_review_passing_workday() -> None:
    result = cr._review_day(
        _cfg(),
        date(2026, 4, 1),
        _valid_workday_pair(),
        TAG_MAP,
        run_id=GUID,
    )
    assert result.ok
    assert result.kind == "workday"
    assert result.replacements == []


def test_review_workday_total_minutes() -> None:
    entries = [
        _pe("e1", _dt(9), _dt(10), "Meeting", tag_id="tag-meeting"),
        _pe("e2", _dt(13), _dt(14), "Doing tasks", tag_id="tag-dev"),
    ]
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("total minutes 120 != 180" in issue for issue in result.issues)
    assert result.replacements


def test_review_workday_outside_window() -> None:
    entries = [
        _pe("e1", _dt(8), _dt(10), "Meeting", tag_id="tag-meeting"),
        _pe("e2", _dt(13), _dt(14), "Doing tasks", tag_id="tag-dev"),
    ]
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("outside work window" in issue for issue in result.issues)


def test_review_workday_step_grid() -> None:
    entries = [
        _pe("e1", _dt(9, 2), _dt(11, 2), "Meeting", tag_id="tag-meeting"),
        _pe("e2", _dt(13), _dt(14), "Doing tasks", tag_id="tag-dev"),
    ]
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("5-minute grid from WORKDAY_START" in issue for issue in result.issues)


def test_review_workday_billable() -> None:
    entries = _valid_workday_pair()
    entries[0].billable = False
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("not billable" in issue for issue in result.issues)


def test_review_workday_description() -> None:
    entries = _valid_workday_pair()
    entries[0].description = "Unknown"
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("is not in DESCRIPTIONS" in issue for issue in result.issues)


def test_review_workday_tag() -> None:
    entries = _valid_workday_pair()
    entries[0].tag_ids = ["tag-dev"]
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("tag should be Meeting" in issue for issue in result.issues)


def test_review_workday_count() -> None:
    entries = [_valid_workday_pair()[0]]
    result = cr._review_day(_cfg(), date(2026, 4, 1), entries, TAG_MAP, run_id=GUID)
    assert any("entry count 1 is outside 2-2" in issue for issue in result.issues)


def test_review_day_off_shape() -> None:
    day = date(2026, 4, 7)
    entries = [
        _pe(
            "leave",
            _dt(9, day=day),
            _dt(17, day=day),
            "Annual leave",
            billable=False,
            tag_id="tag-leave",
        )
    ]
    result = cr._review_day(
        _cfg(day_offs={day}),
        day,
        entries,
        TAG_MAP,
        run_id=GUID,
    )
    assert result.ok
    assert result.kind == "day_off"


def test_review_work_logged_on_leave_day() -> None:
    day = date(2026, 4, 7)
    result = cr._review_day(
        _cfg(day_offs={day}),
        day,
        _valid_workday_pair(day),
        TAG_MAP,
        run_id=GUID,
    )
    assert not result.ok
    assert any("expected exactly one Annual leave entry, found 2" in issue for issue in result.issues)
    assert len(result.replacements) == 1
    assert result.replacements[0].description == "Annual leave"
    assert result.replacements[0].billable is False
    assert result.replacements[0].start == _dt(9, day=day)
    assert result.replacements[0].end == _dt(17, day=day)


def test_review_holiday_shape() -> None:
    day = date(2026, 4, 30)
    entries = [
        _pe(
            "h1",
            _dt(9, day=day),
            _dt(12, day=day),
            "Public holiday",
            billable=False,
            tag_id="tag-holiday",
        )
    ]
    result = cr._review_day(
        _cfg(public_holidays={day}, total_minutes=180),
        day,
        entries,
        TAG_MAP,
        run_id=GUID,
    )
    assert result.ok
    assert result.kind == "public_holiday"


def test_review_weekend_entries_are_delete_only() -> None:
    day = date(2026, 4, 4)
    result = cr._review_day(
        _cfg(),
        day,
        _valid_workday_pair(day),
        TAG_MAP,
        run_id=GUID,
    )
    assert result.kind == "weekend"
    assert any("weekend should have no entries" in issue for issue in result.issues)
    assert result.replacements == []


def test_review_weekend_day_off_expects_no_leave() -> None:
    day = date(2026, 4, 4)
    leave = [
        _pe(
            "leave",
            _dt(9, day=day),
            _dt(17, day=day),
            "Annual leave",
            billable=False,
            tag_id="tag-leave",
        )
    ]
    result = cr._review_day(
        _cfg(day_offs={day}),
        day,
        leave,
        TAG_MAP,
        run_id=GUID,
    )
    assert result.kind == "weekend"
    assert not result.ok
    assert result.replacements == []

    empty = cr._review_day(_cfg(day_offs={day}), day, [], TAG_MAP, run_id=GUID)
    assert empty.ok


def test_load_config_rejects_month_with_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_creds(monkeypatch)
    with pytest.raises(SystemExit, match="either --month or --overwrite"):
        cr.load_config(
            dry_run_cli=False,
            month_cli="2026-04",
            overwrite_cli=GUID,
        )


def test_run_review_saves_guid_and_workday_replacements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env_creds(monkeypatch)
    monkeypatch.setattr(cr, "_today_in_tz", lambda _tz: date(2026, 4, 1))
    _stub_workspace(monkeypatch, tmp_path, existing=[])
    cfg = cr.load_config(dry_run_cli=False, month_cli="2026-04", overwrite_cli=None)
    assert cr.run(cfg) == 0
    out = capsys.readouterr().out
    assert f"Review run: {GUID}" in out
    path = tmp_path / "reviews" / f"{GUID}.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["guid"] == GUID
    assert payload["month"] == "2026-04"
    assert payload["incorrect_days"]
    day = payload["incorrect_days"][0]
    assert day["date"] == "2026-04-01"
    assert day["kind"] == "workday"
    assert len(day["replacements"]) == 2
    total = 0
    for item in day["replacements"]:
        start = cr._parse_clockify_dt(item["start"])
        end = cr._parse_clockify_dt(item["end"])
        total += int((end - start).total_seconds() // 60)
        assert item["billable"] is True
        assert item["description"] in cd.DEFAULT_DESCRIPTIONS
    assert total == 180


def test_overwrite_dry_run_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env_creds(monkeypatch)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / f"{GUID}.json").write_text(
        json.dumps(
            {
                "guid": GUID,
                "month": "2026-04",
                "incorrect_days": [
                    {
                        "date": "2026-04-07",
                        "kind": "day_off",
                        "issues": ["expected exactly one Annual leave entry, found 1"],
                        "entry_ids": ["e1"],
                        "replacements": [
                            {
                                "start": "2026-04-07T09:00:00Z",
                                "end": "2026-04-07T17:00:00Z",
                                "description": "Annual leave",
                                "billable": False,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    deleted: list[str] = []
    created: list[object] = []
    _stub_workspace(
        monkeypatch,
        tmp_path,
        existing=[
            _raw("e1", _dt(9, day=date(2026, 4, 7)), _dt(11, day=date(2026, 4, 7)), "Meeting")
        ],
    )
    monkeypatch.setattr(cr.cd, "delete_time_entry", lambda *a, **k: deleted.append(a[2]))
    monkeypatch.setattr(cr.cd, "create_time_entry", lambda *a, **k: created.append(k))
    cfg = cr.load_config(dry_run_cli=True, month_cli=None, overwrite_cli=GUID)
    assert cr.run(cfg) == 0
    assert deleted == []
    assert created == []


def test_overwrite_deletes_only_incorrect_days_and_posts_stored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env_creds(monkeypatch)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / f"{GUID}.json").write_text(
        json.dumps(
            {
                "guid": GUID,
                "month": "2026-04",
                "incorrect_days": [
                    {
                        "date": "2026-04-07",
                        "kind": "day_off",
                        "issues": ["expected exactly one Annual leave entry, found 1"],
                        "entry_ids": ["e-leave-wrong"],
                        "replacements": [
                            {
                                "start": "2026-04-07T09:00:00Z",
                                "end": "2026-04-07T17:00:00Z",
                                "description": "Annual leave",
                                "billable": False,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    deleted: list[str] = []
    created: list[dict] = []
    listed: list[datetime] = []

    def list_entries(*args: object, **kwargs: object) -> list[dict]:
        listed.append(args[3])  # type: ignore[arg-type]
        return [
            _raw(
                "e-leave-wrong",
                _dt(9, day=date(2026, 4, 7)),
                _dt(11, day=date(2026, 4, 7)),
                "Meeting",
                tag_ids=["tag-meeting"],
            )
        ]

    _stub_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(cr.cd, "list_time_entries", list_entries)
    monkeypatch.setattr(cr.cd, "delete_time_entry", lambda *a, **k: deleted.append(a[2]))

    def capture_create(*_a: object, **kwargs: object) -> dict:
        created.append(kwargs)
        return {"id": "new"}

    monkeypatch.setattr(cr.cd, "create_time_entry", capture_create)
    cfg = cr.load_config(dry_run_cli=False, month_cli=None, overwrite_cli=GUID)
    assert cr.run(cfg) == 0
    assert deleted == ["e-leave-wrong"]
    assert len(listed) == 1
    assert len(created) == 1
    assert created[0]["description"] == "Annual leave"
    assert created[0]["billable"] is False
    assert created[0]["start"] == datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
    assert created[0]["end"] == datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc)
    assert created[0]["tag_ids"] == ["tag-leave"]


def test_overwrite_missing_run_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env_creds(monkeypatch)
    monkeypatch.setattr(cr, "REVIEWS_DIR", tmp_path / "reviews")
    cfg = cr.load_config(dry_run_cli=False, month_cli=None, overwrite_cli=GUID)
    with pytest.raises(SystemExit, match="Review run not found"):
        cr.run(cfg)


def test_missing_workspace_tags_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env_creds(monkeypatch)
    monkeypatch.setattr(cr, "_today_in_tz", lambda _tz: date(2026, 4, 1))
    _stub_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(cr.cd, "list_workspace_tags", lambda *_a, **_k: [])
    cfg = cr.load_config(dry_run_cli=False, month_cli="2026-04", overwrite_cli=None)
    with pytest.raises(SystemExit, match="Missing workspace tags"):
        cr.run(cfg)
