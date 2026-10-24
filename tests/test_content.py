"""Content describes itself; its projection carries that and nothing else (#1220)."""

from __future__ import annotations

import pytest

from rey_lib.content import Content
from rey_lib.errors.error_utils import ConfigError
from rey_lib.presentation.projection import Projection


def test_the_projection_is_the_content_under_one_key() -> None:
    content = Content(name="add_group_tab", text="# Add a group tab", language="markdown",
                      html="<h1>Add a group tab</h1>", html_trusted=True)

    assert content.projection() == {"content": {
        "name": "add_group_tab", "text": "# Add a group tab", "language": "markdown",
        "html": "<h1>Add a group tab</h1>", "html_trusted": True, "refusal": "",
        "truncated": False,
    }}


def test_plain_content_carries_no_html_and_is_not_truncated() -> None:
    projected = Content(name="notes.txt", text="hello", language="text").projection()["content"]

    assert (projected["html"], projected["html_trusted"], projected["truncated"]) \
        == ("", False, False)


def test_a_refusal_is_carried_without_text() -> None:
    projected = Content(name=".env", text="", language="text",
                        refusal="Sensitive file preview is blocked.").projection()["content"]

    assert projected["refusal"] == "Sensitive file preview is blocked."
    assert projected["text"] == ""


def test_the_projection_names_no_host_subject_or_panel() -> None:
    projected = Content(name="a.yaml", text="a: 1", language="yaml").projection()

    assert set(projected) == {"content"}
    assert not {"subject_key", "panel", "panel_settings"} & set(projected["content"])


def test_a_host_hands_the_projection_to_the_subjects_declared_presentation() -> None:
    class _Control:
        def subject_panel(self, subject: str) -> dict:
            return {"subject_key": subject, "panel": "shared_file_viewer", "panel_settings": {}}

    content = Content(name="a.md", text="# A", language="markdown")

    projection = Projection.for_subject(_Control(), "content", content.projection())

    assert projection.to_dict()["content"] == content.projection()["content"]


@pytest.mark.parametrize(("fields", "refusal"), [
    ({"name": "", "text": "x", "language": "text"}, "needs a name"),
    ({"name": "  ", "text": "x", "language": "text"}, "needs a name"),
    ({"name": "a", "text": "x", "language": "text/markdown"}, "not an approved text language"),
    ({"name": "a", "text": "x", "language": "text", "html_trusted": True}, "carries none"),
    ({"name": "a", "text": "x", "language": "text", "refusal": "no"}, "carries no text"),
])
def test_content_that_does_not_describe_itself_is_refused(fields: dict, refusal: str) -> None:
    with pytest.raises(ConfigError, match=refusal):
        Content(**fields)
