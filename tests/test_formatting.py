"""Tests for shared application-neutral formatting."""

from rey_lib.formatting import markdown_to_html


def test_markdown_to_html_returns_semantic_fragment() -> None:
    result = markdown_to_html(
        "# Result\n\n- **failed** at `step_1`\n- See [details](https://example.com)\n"
    )

    assert "<h1>Result</h1>" in result
    assert "<ul>" in result and "</ul>" in result
    assert "<strong>failed</strong>" in result
    assert "<code>step_1</code>" in result
    assert '<a href="https://example.com">details</a>' in result
    assert "<html" not in result and "<body" not in result


def test_markdown_to_html_handles_empty_input_and_escapes_raw_html() -> None:
    assert markdown_to_html("") == ""
    assert markdown_to_html("<script>alert(1)</script>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>\n"
    )


def test_markdown_to_html_unwraps_one_outer_markdown_document_fence() -> None:
    result = markdown_to_html(
        "```markdown\n# Pipeline Result\n\n## Summary\n\n- **Status:** failed\n```\n"
    )

    assert "<h1>Pipeline Result</h1>" in result
    assert "<h2>Summary</h2>" in result
    assert "<pre><code" not in result
