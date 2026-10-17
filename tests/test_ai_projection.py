"""AI states what it offers an installation, as plain data (backlog 1041).

What an ai_app session sends its presentation: the governed tasks, in configured
order, each with its key, label and whether it is composed first. Read through the
Control's existing AI configuration read; nothing else re-derives it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.ai import AI
from rey_lib.ai.registry import AIRegistry


class _Control:
    """The one read projection uses: control.f_ai_configuration_get's rows."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        self.asked.append(installation)
        return [
            {"row_type": "group", "task_key": None, "task_label": None, "is_enabled": True,
             "composed_first": False},
            {"row_type": "task", "task_key": "search_index", "task_label": "Search the index",
             "is_enabled": True, "composed_first": True},
            {"row_type": "task", "task_key": "retired", "task_label": "Retired",
             "is_enabled": False, "composed_first": False},
            {"row_type": "task", "task_key": "generate_sql", "task_label": None,
             "is_enabled": True, "composed_first": None},
        ]


def test_the_projection_offers_the_enabled_governed_tasks_in_order() -> None:
    control = _Control()

    projection = AI(registry=AIRegistry()).projection(control, "local")

    assert control.asked == ["local"]
    assert projection == {"tasks": [
        {"task_key": "search_index", "label": "Search the index", "composed_first": True},
        {"task_key": "generate_sql", "label": "generate_sql", "composed_first": False},
    ]}
