"""One running application hosts many objects by opaque id (#1223)."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.run.hosted_objects import HostedObjects


class _Channel:
    """A launcher channel stand-in: the commands it sends, the messages emitted."""

    def __init__(self, commands: list[dict[str, Any]]) -> None:
        self._commands = commands
        self.emitted: list[dict[str, Any]] = []

    def commands(self) -> list[dict[str, Any]]:
        return self._commands

    def emit(self, message: dict[str, Any]) -> None:
        self.emitted.append(message)


def _construct(subject: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    if subject != "application.settings":
        raise ConfigError(f"No subject {subject!r} here.")
    return {"application_id": identity.get("application_id")}


def _project(subject: str, held: dict[str, Any]) -> dict[str, Any]:
    return {"subject_key": subject, **held}


def _hosted() -> HostedObjects:
    return HostedObjects(_construct, _project)


def test_each_open_holds_a_new_object_under_an_opaque_id() -> None:
    hosted = _hosted()

    first, projected = hosted.open("application.settings", {"application_id": "file_operator"})
    second, _ = hosted.open("application.settings", {"application_id": "ai_app"})

    assert first != second and len(hosted) == 2
    assert projected == {"subject_key": "application.settings", "application_id": "file_operator"}
    assert hosted.projection(second)["application_id"] == "ai_app"


def test_releasing_an_object_ends_that_object_only() -> None:
    hosted = _hosted()
    first, _ = hosted.open("application.settings", {"application_id": "file_operator"})
    second, _ = hosted.open("application.settings", {"application_id": "ai_app"})

    hosted.release(first)

    assert len(hosted) == 1
    assert hosted.projection(second)["application_id"] == "ai_app"
    with pytest.raises(ConfigError, match="No object is held"):
        hosted.projection(first)


def test_the_protocol_names_every_answer_by_its_object() -> None:
    hosted = _hosted()
    channel = _Channel([{"command": "open", "subject": "application.settings",
                         "identity": {"application_id": "file_operator"}, "request_id": "r1"}])

    hosted.serve(channel)
    (opened,) = channel.emitted
    object_id = opened["object"]
    channel = _Channel([{"command": "projection", "object": object_id},
                        {"command": "release", "object": object_id, "request_id": "r2"},
                        {"command": "close"},
                        {"command": "projection", "object": object_id}])
    hosted.serve(channel)

    assert opened["request_id"] == "r1" and opened["projection"]["application_id"] == "file_operator"
    assert channel.emitted == [
        {"object": object_id, "projection": opened["projection"]},
        {"object": object_id, "released": True, "request_id": "r2"},
    ]


def test_a_refusal_is_answered_and_serving_goes_on(monkeypatch) -> None:
    monkeypatch.setattr(ConfigError, "report", lambda self, *_a, **_k: None)
    channel = _Channel([
        {"command": "open", "subject": "recipe.document", "identity": {}, "request_id": "r1"},
        {"command": "projection", "object": "nope", "request_id": "r2"},
        {"command": "execute", "request_id": "r3"},
    ])

    _hosted().serve(channel)

    assert [one["request_id"] for one in channel.emitted] == ["r1", "r2", "r3"]
    assert all("error" in one for one in channel.emitted)
    assert channel.emitted[1]["object"] == "nope"
