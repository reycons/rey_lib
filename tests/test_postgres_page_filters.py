"""The Postgres provider renders a logical nested-key filter (#1143).

Rendering only, without a server: the clause it writes and the values it binds.
The keys are bound as one text array and never written into the statement; the
nested value is compared as its text.
"""

from __future__ import annotations

import pytest

from rey_lib.db.postgres_utils import _rendered_filters
from rey_lib.errors.error_utils import DatabaseError


def test_a_column_filter_is_unchanged() -> None:
    clause, values = _rendered_filters(
        [{"column": "run_id", "operator": "equals", "value": 15}])
    assert clause == '\nWHERE  "run_id" = %s'
    assert values == [15]


def test_a_nested_key_filter_binds_its_path_and_compares_text() -> None:
    clause, values = _rendered_filters([
        {"column": "doc", "path": ["values", "feed"], "operator": "equals", "value": "alpha"},
        {"column": "doc", "path": ["n"], "operator": "gt", "value": 5},
    ])
    assert clause == ('\nWHERE  ("doc" #>> %s::text[]) = %s'
                      '\n   AND ("doc" #>> %s::text[]) > %s')
    assert values == [["values", "feed"], "alpha", ["n"], "5"]
    assert "feed" not in clause


@pytest.mark.parametrize("path", [[], "values.feed", ["values", ""], ["values", 1]])
def test_a_path_that_is_not_a_list_of_keys_is_refused(path: object) -> None:
    with pytest.raises(DatabaseError, match="path must be a list of non-empty keys"):
        _rendered_filters([{"column": "doc", "path": path, "operator": "equals", "value": "a"}])
