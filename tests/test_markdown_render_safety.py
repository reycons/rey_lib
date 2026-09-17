"""What the estate's Markdown renderer refuses to produce.

The Console draws a document it generated into its own page, so that the
document follows the theme rather than rendering in an isolated frame on the
browser's own white. That is only safe because of what this renderer will not
emit, and those are facts about a dependency rather than about our code -- so
they are asserted here, where a version bump that changed them fails loudly.

Two properties, and one is not enough on its own:

* raw HTML is escaped, because the renderer runs with ``html: False``;
* a hostile URL never becomes an ``href`` or a ``src``, because markdown-it
  validates links. Escaping raw HTML would not cover this: ``[x](javascript:…)``
  contains no HTML at all, and an anchor is generated from ordinary Markdown.

If one of these fails, the Console's generated branch must sanitize at the
render boundary before inserting. It must not keep inserting.
"""

from __future__ import annotations

import re

import pytest

from rey_lib.formatting import markdown_to_html


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<iframe src='https://example.com'></iframe>",
        "<a href='https://example.com' onclick='alert(1)'>x</a>",
    ],
)
def test_raw_html_is_escaped_rather_than_emitted(source: str) -> None:
    """Nothing a document says in raw HTML becomes a tag.

    Asserted on the tags produced, not on substrings. Escaped output still
    *contains* the word ``onerror`` -- as text, which is the harmless case and
    the whole point -- so a substring check would fail on correct behaviour and
    teach whoever hit it to weaken the test.
    """
    rendered = markdown_to_html(source)

    produced = {match.lower() for match in re.findall(r"</?([a-zA-Z][\w-]*)", rendered)}
    assert produced <= {"p"}, f"raw HTML produced tags: {sorted(produced - {'p'})}"
    # Escaped to text, which is the visible proof it was not parsed as markup.
    assert "&lt;" in rendered


@pytest.mark.parametrize(
    "source",
    [
        "[click](javascript:alert(1))",
        "[click](JaVaScRiPt:alert(1))",
        "[click](vbscript:msgbox(1))",
        "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        "![image](javascript:alert(1))",
        "<javascript:alert(1)>",
    ],
)
def test_a_hostile_url_never_becomes_an_attribute(source: str) -> None:
    """Refused at the link, not merely escaped inside one.

    The whole point is that no ``href`` or ``src`` carrying these schemes is
    produced. markdown-it declines to make the link at all and leaves the text
    as written, which is why the rendered fragment is safe to insert.
    """
    rendered = markdown_to_html(source).lower()

    assert 'href="javascript:' not in rendered
    assert 'href="vbscript:' not in rendered
    assert 'href="data:text/html' not in rendered
    assert 'src="javascript:' not in rendered
    assert 'src="vbscript:' not in rendered


def test_an_ordinary_link_still_renders() -> None:
    """The refusal is of schemes, not of links.

    A recipe that cites a URL should link it; a test that only proved things
    were blocked would pass just as well against a renderer that emitted
    nothing at all.
    """
    rendered = markdown_to_html("[docs](https://example.com/a?b=1)")

    assert '<a href="https://example.com/a?b=1">docs</a>' in rendered


def test_an_inline_image_survives_for_the_schemes_that_are_allowed() -> None:
    """``data:`` is refused for documents and allowed for images.

    Stated because it is the one place the rule is not simply "no data URLs",
    and a later reader should not tighten it by accident believing it was.
    """
    rendered = markdown_to_html("![dot](data:image/png;base64,iVBORw0KGgo=)")

    assert 'src="data:image/png;base64,iVBORw0KGgo="' in rendered


def test_a_table_renders_because_the_estate_enabled_it() -> None:
    """Tables are a GFM extension this renderer turns on, and recipes use them."""
    rendered = markdown_to_html("| a | b |\n| - | - |\n| 1 | 2 |")

    assert "<table>" in rendered
    assert "<th>" in rendered
