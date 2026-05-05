"""Tests for clockify_tag_existing (no network)."""

from __future__ import annotations

from datetime import date

import pytest
from zoneinfo import ZoneInfo

import clockify_tag_existing as cte
import clockify_tags as ct


def test_tags_by_name_extracts_valid_pairs() -> None:
    out = ct.tags_by_name(
        [
            {"id": "t1", "name": "Meeting"},
            {"id": "t2", "name": "Development"},
            {"id": 123, "name": "Invalid"},
        ]
    )
    assert out == {"Meeting": "t1", "Development": "t2"}


def test_load_config_requires_both_range_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(SystemExit, match="Both START_DATE and END_DATE"):
        cte.load_config(
            dry_run_cli=True,
            target_date_cli=None,
            start_date_cli="2026-04-01",
            end_date_cli=None,
        )


def test_run_updates_matching_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cte.Config(
        api_key="secret",
        workspace_id="ws",
        tz=ZoneInfo("UTC"),
        target_date=date(2026, 4, 21),
        start_date=None,
        end_date=None,
        dry_run=False,
    )

    monkeypatch.setattr(cte, "get_user", lambda _api: {"id": "u1"})
    monkeypatch.setattr(
        cte,
        "list_workspace_tags",
        lambda _api, _ws: [
            {"id": "tag-meeting", "name": "Meeting"},
            {"id": "tag-dev", "name": "Development"},
            {"id": "tag-bug", "name": "Bug fixes"},
            {"id": "tag-review", "name": "Code Review"},
            {"id": "tag-doc", "name": "Documenting"},
            {"id": "tag-leave", "name": "On-Leave"},
            {"id": "tag-holiday", "name": "Public Holiday"},
        ],
    )
    monkeypatch.setattr(
        cte,
        "list_time_entries",
        lambda *_a, **_k: [
            {"id": "e1", "description": "Meeting", "tagIds": []},
            {"id": "e2", "description": "Doing tasks", "tagIds": ["existing"]},
            {"id": "e3", "description": "Random task", "tagIds": []},
            {"id": "e4", "description": "Planning", "tagIds": ["tag-meeting"]},
            {"id": "e5", "description": "Day off", "tagIds": []},
            {"id": "e6", "description": "Public holiday", "tagIds": ["existing2"]},
        ],
    )

    updated: list[tuple[str, list[str]]] = []

    def _capture(
        _api: str, _ws: str, entry_id: str, tag_ids: list[str], *, entry: dict[str, object] | None = None
    ) -> dict[str, str]:
        updated.append((entry_id, tag_ids))
        return {"id": entry_id}

    monkeypatch.setattr(cte, "update_time_entry_tags", _capture)

    code = cte.run(cfg)
    assert code == 0
    assert updated == [
        ("e1", ["tag-meeting"]),
        ("e2", ["existing", "tag-dev"]),
        ("e5", ["tag-leave"]),
        ("e6", ["existing2", "tag-holiday"]),
    ]


def test_run_fails_when_required_workspace_tags_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cte.Config(
        api_key="secret",
        workspace_id="ws",
        tz=ZoneInfo("UTC"),
        target_date=date(2026, 4, 21),
        start_date=None,
        end_date=None,
        dry_run=False,
    )
    monkeypatch.setattr(cte, "get_user", lambda _api: {"id": "u1"})
    monkeypatch.setattr(cte, "list_workspace_tags", lambda _api, _ws: [{"id": "t1", "name": "Meeting"}])
    code = cte.run(cfg)
    assert code == 1


def test_update_time_entry_tags_put_fallback_for_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_http_json(
        method: str,
        _url: str,
        *,
        api_key: str,
        body: dict[str, object] | None = None,
    ) -> object:
        assert api_key == "secret"
        calls.append((method, body or {}))
        if len(calls) == 1:
            raise RuntimeError("HTTP 400 PUT some-url: invalid payload")
        return {"id": "entry-1"}

    monkeypatch.setattr(cte, "_http_json", _fake_http_json)
    out = cte.update_time_entry_tags(
        "secret",
        "ws",
        "entry-1",
        ["tag-1"],
        entry={
            "description": "Meeting",
            "billable": True,
            "projectId": "proj",
            "taskId": "task",
            "timeInterval": {"start": "2026-04-01T01:00:00Z", "end": "2026-04-01T02:00:00Z"},
        },
    )
    assert out == {"id": "entry-1"}
    assert calls[0] == ("PUT", {"tagIds": ["tag-1"]})
    assert calls[1][0] == "PUT"
    assert calls[1][1]["start"] == "2026-04-01T01:00:00Z"
    assert calls[1][1]["end"] == "2026-04-01T02:00:00Z"
    assert calls[1][1]["projectId"] == "proj"
    assert calls[1][1]["taskId"] == "task"
