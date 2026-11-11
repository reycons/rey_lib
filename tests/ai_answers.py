"""What a model answers for a task with no structured output: the artifact envelope.

Every such task's model declares what it produced (rey_lib.artifacts.envelope), so a
stand-in engine answers the same way.
"""

from __future__ import annotations

import json
from typing import Any


def enveloped(content: str, artifact_type: str = "text", notes: Any = ()) -> str:
    """``content`` as a model declaring it as ``artifact_type`` answers it."""
    return json.dumps({"artifact_type": artifact_type, "content": content,
                       "notes": list(notes)})
