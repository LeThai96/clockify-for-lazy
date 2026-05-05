"""Shared Clockify tag mapping and lookup helpers."""

from __future__ import annotations

from typing import Any

DESCRIPTION_TO_TAG_NAME = {
    "Meeting": "Meeting",
    "Fixing bugs": "Bug fixes",
    "Analyze requirements": "Development",
    "Doing tasks": "Development",
    "Code review": "Code review",
    "Documentation": "Documenting",
    "Planning": "Meeting",
    "Sync with team": "Meeting",
    "Day off": "On-Leave",
    "Public holiday": "Public Holiday",
}


def tags_by_name(tags: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in tags:
        name = tag.get("name")
        tag_id = tag.get("id")
        if isinstance(name, str) and isinstance(tag_id, str):
            out[name] = tag_id
    return out


def resolve_tag_id(tag_name_to_id: dict[str, str], mapped_tag_name: str) -> str | None:
    exact = tag_name_to_id.get(mapped_tag_name)
    if exact is not None:
        return exact
    target = mapped_tag_name.casefold()
    for name, tag_id in tag_name_to_id.items():
        if name.casefold() == target:
            return tag_id
    return None


def required_tag_names() -> list[str]:
    return sorted(set(DESCRIPTION_TO_TAG_NAME.values()))


def find_missing_tag_names(tag_name_to_id: dict[str, str]) -> list[str]:
    return [tag_name for tag_name in required_tag_names() if resolve_tag_id(tag_name_to_id, tag_name) is None]
