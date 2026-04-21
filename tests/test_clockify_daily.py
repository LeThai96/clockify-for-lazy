"""Tests for clockify_daily (no network)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import pytest
from zoneinfo import ZoneInfo

import clockify_daily as cd


def test_parse_hhmm() -> None:
    assert cd._parse_hhmm("09:30") == (9, 30)
    with pytest.raises(ValueError):
        cd._parse_hhmm("9:30:00")
    with pytest.raises(ValueError):
        cd._parse_hhmm("25:00")


def test_utc_iso() -> None:
    dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert cd._utc_iso(dt) == "2024-01-15T12:00:00Z"
    dt_hcm = datetime(2024, 1, 15, 19, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert cd._utc_iso(dt_hcm) == "2024-01-15T12:00:00Z"


def test_mask_api_key() -> None:
    assert cd._mask_api_key("short") == "***"
    long_k = "abcdefghijklmnop"
    masked = cd._mask_api_key(long_k)
    assert "abcd" in masked and "mnop" in masked and "redacted" in masked


def test_time_entries_url() -> None:
    s = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    e = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    url = cd._time_entries_url("ws", "uid", s, e)
    assert "workspaces/ws/user/uid/time-entries" in url
    assert "start=" in url and "end=" in url


def test_generate_intervals_non_overlapping() -> None:
    tz = ZoneInfo("UTC")
    day = date(2024, 6, 1)
    w0 = datetime.combine(day, time(9, 0), tzinfo=tz)
    w1 = datetime.combine(day, time(18, 0), tzinfo=tz)
    rng = __import__("random").Random(12345)
    cfg = cd.Config(
        api_key="",
        workspace_id="",
        project_id=None,
        tz=tz,
        work_start=time(9, 0),
        work_end=time(18, 0),
        min_entries=3,
        max_entries=3,
        min_minutes=30,
        max_minutes=60,
        total_minutes=150,
        step_minutes=5,
        descriptions=["x"],
        ignore_day_has_entries=False,
        dry_run=True,
        debug=False,
        target_date=None,
        start_date=None,
        end_date=None,
        public_holidays=set(),
    )
    intervals = cd._generate_intervals(rng, w0, w1, cfg)
    assert len(intervals) == 3
    total = 0
    for a, b in zip(intervals, intervals[1:]):
        assert a[1] <= b[0]
    for s, e in intervals:
        assert s.minute % 5 == 0
        assert e.minute % 5 == 0
        total += int((e - s).total_seconds() // 60)
    assert total == 150


def test_load_config_debug_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep explicit empty vars so load_dotenv(override=False) does not inject from local .env.
    monkeypatch.setenv("CLOCKIFY_API_KEY", "")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "")
    monkeypatch.setenv("CLOCKIFY_DEBUG", "1")
    with pytest.raises(SystemExit, match="CLOCKIFY_DEBUG requires"):
        cd.load_config(
            dry_run_cli=False,
            debug_cli=True,
            target_date_cli=None,
            start_date_cli=None,
            end_date_cli=None,
        )


def test_load_config_target_date_from_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "workspace-xyz")
    cfg = cd.load_config(
        dry_run_cli=False,
        debug_cli=False,
        target_date_cli="2026-01-01",
        start_date_cli=None,
        end_date_cli=None,
    )
    assert cfg.target_date == date(2026, 1, 1)


def test_load_config_invalid_target_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(SystemExit, match="Invalid target date"):
        cd.load_config(
            dry_run_cli=True,
            debug_cli=False,
            target_date_cli="01-01-2026",
            start_date_cli=None,
            end_date_cli=None,
        )


def test_run_debug_prints_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "workspace-xyz")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("MIN_ENTRIES", "2")
    monkeypatch.setenv("MAX_ENTRIES", "2")
    monkeypatch.setenv("MIN_MINUTES", "45")
    monkeypatch.setenv("MAX_MINUTES", "45")
    monkeypatch.setenv("TOTAL_MINUTES", "90")
    monkeypatch.setenv("CLOCKIFY_RANDOM_SEED", "99")

    fixed = date(2024, 3, 10)
    monkeypatch.setattr(cd, "_today_in_tz", lambda _tz: fixed)

    cfg = cd.load_config(
        dry_run_cli=False,
        debug_cli=True,
        target_date_cli=None,
        start_date_cli=None,
        end_date_cli=None,
    )
    assert cd.run(cfg) == 0
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert "GET https://api.clockify.me/api/v1/user" in out
    assert "workspace-xyz/user/<userId>/time-entries" in out
    assert "POST https://api.clockify.me/api/v1/workspaces/workspace-xyz/time-entries" in out
    assert "billable" in out
    assert "Clockify debug mode" in err
    assert "abcd...mnop" in out and "redacted" in out


def test_run_skips_when_day_has_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.delenv("CLOCKIFY_DEBUG", raising=False)

    monkeypatch.setattr(cd, "_today_in_tz", lambda _tz: date(2024, 5, 1))
    monkeypatch.setattr(cd, "get_user", lambda _k: {"id": "u1"})
    monkeypatch.setattr(cd, "list_time_entries", lambda *a, **k: [{"id": "e1"}])

    created: list[object] = []

    def capture_create(*a, **k):
        created.append((a, k))
        return {"id": "new"}

    monkeypatch.setattr(cd, "create_time_entry", capture_create)

    cfg = cd.load_config(
        dry_run_cli=False,
        debug_cli=False,
        target_date_cli=None,
        start_date_cli=None,
        end_date_cli=None,
    )
    assert cd.run(cfg) == 0
    assert created == []


def test_run_creates_entries_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("MIN_ENTRIES", "2")
    monkeypatch.setenv("MAX_ENTRIES", "2")
    monkeypatch.setenv("TOTAL_MINUTES", "120")
    monkeypatch.setenv("CLOCKIFY_RANDOM_SEED", "42")
    monkeypatch.delenv("CLOCKIFY_DEBUG", raising=False)

    monkeypatch.setattr(cd, "_today_in_tz", lambda _tz: date(2024, 5, 2))
    monkeypatch.setattr(cd, "get_user", lambda _k: {"id": "u1"})
    monkeypatch.setattr(cd, "list_time_entries", lambda *a, **k: [])

    created: list[object] = []

    def capture_create(*a, **k):
        created.append((a, k))
        return {"id": "new"}

    monkeypatch.setattr(cd, "create_time_entry", capture_create)

    cfg = cd.load_config(
        dry_run_cli=False,
        debug_cli=False,
        target_date_cli=None,
        start_date_cli=None,
        end_date_cli=None,
    )
    assert cd.run(cfg) == 0
    assert len(created) == 2
    total = 0
    for _args, kwargs in created:
        start = kwargs["start"]
        end = kwargs["end"]
        assert start.minute % 5 == 0
        assert end.minute % 5 == 0
        total += int((end - start).total_seconds() // 60)
    assert total == 120


def test_http_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        def read(self) -> bytes:
            return b'{"id":"x","name":"y"}'

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(req: object, timeout: int = 60) -> FakeResp:
        return FakeResp()

    monkeypatch.setattr(cd.urllib.request, "urlopen", fake_urlopen)

    out = cd._http_json("GET", "https://example.com", api_key="secret")
    assert out == {"id": "x", "name": "y"}


def test_target_days_range_and_holidays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCKIFY_API_KEY", "abcdefghijklmnop")
    monkeypatch.setenv("CLOCKIFY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("MIN_ENTRIES", "2")
    monkeypatch.setenv("MAX_ENTRIES", "2")
    monkeypatch.setenv("TOTAL_MINUTES", "120")
    monkeypatch.setenv("CLOCKIFY_TARGET_DATE", "")
    monkeypatch.setenv("PUBLIC_HOLIDAYS", "2026-04-06")
    monkeypatch.delenv("CLOCKIFY_DEBUG", raising=False)

    monkeypatch.setattr(cd, "get_user", lambda _k: {"id": "u1"})
    monkeypatch.setattr(cd, "list_time_entries", lambda *a, **k: [])

    created: list[object] = []

    def capture_create(*a, **k):
        created.append((a, k))
        return {"id": "new"}

    monkeypatch.setattr(cd, "create_time_entry", capture_create)

    cfg = cd.load_config(
        dry_run_cli=False,
        debug_cli=False,
        target_date_cli=None,
        start_date_cli="2026-04-03",
        end_date_cli="2026-04-07",
    )
    assert cd.run(cfg) == 0
    # Range Fri..Tue with weekend + holiday on Monday => only Fri and Tue are logged.
    # 2 entries/day by config => total 4 created entries.
    assert len(created) == 4
