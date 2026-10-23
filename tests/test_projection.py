"""Projection.for_subject: a resolved subject's governed presentation (backlog 1215).

The declaration is read through Control.subject_panel and carried; the content is
the owner's and carried unread. A subject with no declaration is refused, never
defaulted. These use a stand-in Control holding the file.yaml row as governed.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.presentation import Projection

#: The file.yaml declaration as tree.subject_panel holds it.
_FILE_YAML = {"subject_panel_id": 24, "subject_key": "file.yaml",
              "panel": "shared_file_viewer", "panel_settings": {"language": "yaml"}}


class _Control:
    """Answers Control.subject_panel from a fixed set of governed rows."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self._rows = {row["subject_key"]: row for row in rows}
        self.asked: list[str] = []

    def subject_panel(self, subject_key: str) -> Optional[dict[str, Any]]:
        self.asked.append(subject_key)
        row = self._rows.get(subject_key)
        return dict(row) if row is not None else None


def test_a_declared_subject_projects_its_panel_settings_and_content() -> None:
    control = _Control(_FILE_YAML)
    content = {"file": {"file_type": "YAML", "content": "a: 1\n"}}

    projection = Projection.for_subject(control, "file.yaml", content)

    assert projection.subject == "file.yaml"
    assert projection.panel == "shared_file_viewer"
    assert projection.panel_settings == {"language": "yaml"}
    assert projection.content == content
    assert control.asked == ["file.yaml"]


def test_it_travels_as_its_declaration_beside_its_content() -> None:
    projection = Projection.for_subject(
        _Control(_FILE_YAML), "file.yaml", {"file": {"content": "a: 1\n"}},
    )

    assert projection.to_dict() == {
        "subject": "file.yaml",
        "panel": "shared_file_viewer",
        "panel_settings": {"language": "yaml"},
        "file": {"content": "a: 1\n"},
    }


def test_an_undeclared_subject_is_refused_by_name() -> None:
    with pytest.raises(ConfigError) as raised:
        Projection.for_subject(_Control(_FILE_YAML), "file.csv", {})

    assert "file.csv" in str(raised.value)


def test_no_subject_is_refused_before_anything_is_read() -> None:
    control = _Control(_FILE_YAML)

    with pytest.raises(ConfigError):
        Projection.for_subject(control, "  ", {})
    assert control.asked == []


def test_content_may_not_name_what_the_projection_carries() -> None:
    with pytest.raises(ConfigError) as raised:
        Projection.for_subject(_Control(_FILE_YAML), "file.yaml", {"panel": "json_viewer"})

    assert "panel" in str(raised.value)


@pytest.mark.parametrize("row", [
    {"subject_key": "file.yaml", "panel": "", "panel_settings": {}},
    {"subject_key": "file.yaml", "panel": "shared_file_viewer", "panel_settings": None},
])
def test_a_malformed_declaration_is_refused(row: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        Projection.for_subject(_Control(row), "file.yaml", {})


def test_projecting_carries_the_same_declaration_over_new_content() -> None:
    """One lookup when the subject is established; each re-projection reuses it."""
    control = _Control(_FILE_YAML)
    declared = Projection.for_subject(control, "file.yaml", {})

    projected = declared.projecting({"file": {"content": "b: 2\n"}})

    assert projected.to_dict() == {
        "subject": "file.yaml", "panel": "shared_file_viewer",
        "panel_settings": {"language": "yaml"}, "file": {"content": "b: 2\n"},
    }
    assert control.asked == ["file.yaml"]
    with pytest.raises(ConfigError):
        declared.projecting({"panel_settings": {}})


def test_the_projection_does_not_resolve_the_subject() -> None:
    """It consumes the resolved key as given: no file, path or format is read."""
    control = _Control(_FILE_YAML)

    Projection.for_subject(control, "file.yaml", {})

    assert control.asked == ["file.yaml"]
