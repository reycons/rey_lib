"""One running application hosts many objects by opaque id (#1223)."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.run.hosted_objects import Emit, HostedObjects


class _Channel:
    """A launcher channel stand-in: the commands it sends, the messages emitted."""

    def __init__(self, commands: list[dict[str, Any]]) -> None:
        self._commands = commands
        self.emitted: list[dict[str, Any]] = []

    def commands(self) -> list[dict[str, Any]]:
        return self._commands

    def emit(self, message: dict[str, Any]) -> None:
        self.emitted.append(message)


class _Settings:
    """A hosted object stand-in: one application's settings, with one command of its own."""

    released: list[str] = []

    def __init__(self, application_id: str, emit: Emit) -> None:
        self.application_id = application_id
        self.emit = emit
        self.renamed = ""

    def projection(self) -> dict[str, Any]:
        return {"application_id": self.application_id, "renamed": self.renamed}

    def answer(self, command: dict[str, Any]) -> None:
        if command.get("command") != "rename":
            raise ConfigError(f"settings do not answer {command.get('command')!r}")
        self.renamed = str(command.get("to") or "")
        self.emit({"projection": self.projection(), "request_id": command.get("request_id")})

    def release(self) -> None:
        _Settings.released.append(self.application_id)


def _construct(subject: str, identity: Mapping[str, Any], emit: Emit) -> _Settings:
    if subject != "application.settings":
        raise ConfigError(f"No subject {subject!r} here.")
    return _Settings(str(identity.get("application_id")), emit)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch) -> None:
    _Settings.released = []
    monkeypatch.setattr(ConfigError, "report", lambda self, *_a, **_k: None)


def _open(application_id: str, request_id: str) -> dict[str, Any]:
    return {"command": "open", "subject": "application.settings",
            "identity": {"application_id": application_id}, "request_id": request_id}


def test_each_open_holds_a_new_object_under_an_opaque_id() -> None:
    hosted = HostedObjects(_construct)
    channel = _Channel([_open("file_operator", "r1"), _open("ai_app", "r2")])

    hosted.serve(channel)

    first, second = channel.emitted
    assert first["object"] != second["object"]
    assert first["projection"]["application_id"] == "file_operator"
    assert (first["request_id"], second["request_id"]) == ("r1", "r2")


def test_an_objects_own_command_is_answered_by_it_and_names_it() -> None:
    hosted = HostedObjects(_construct)
    channel = _Channel([])
    object_id, _ = hosted.open("application.settings", {"application_id": "file_operator"},
                               channel.emit)
    channel._commands.append({"command": "rename", "object": object_id, "to": "fo",
                              "request_id": "r2"})

    hosted.serve(channel)

    assert channel.emitted == [{"projection": {"application_id": "file_operator",
                                               "renamed": "fo"},
                                "request_id": "r2", "object": object_id}]


def test_releasing_an_object_ends_that_object_only_and_serving_ending_releases_the_rest() -> None:
    hosted = HostedObjects(_construct)
    first, _ = hosted.open("application.settings", {"application_id": "file_operator"},
                           lambda message: None)
    second, _ = hosted.open("application.settings", {"application_id": "ai_app"},
                            lambda message: None)

    hosted.release(first)
    assert _Settings.released == ["file_operator"] and len(hosted) == 1
    assert hosted.projection(second)["application_id"] == "ai_app"
    with pytest.raises(ConfigError, match="No object is held"):
        hosted.projection(first)

    hosted.serve(_Channel([{"command": "close"}]))
    assert _Settings.released == ["file_operator", "ai_app"] and len(hosted) == 0


def test_a_refusal_is_answered_and_serving_goes_on() -> None:
    hosted = HostedObjects(_construct)
    channel = _Channel([
        {"command": "open", "subject": "recipe.document", "identity": {}, "request_id": "r1"},
        {"command": "projection", "object": "nope", "request_id": "r2"},
        {"command": "rename", "object": "nope", "request_id": "r3"},
        _open("ai_app", "r4"),
    ])

    hosted.serve(channel)

    assert [one["request_id"] for one in channel.emitted] == ["r1", "r2", "r3", "r4"]
    assert all("error" in one for one in channel.emitted[:3])
    assert channel.emitted[1]["object"] == "nope"
    assert channel.emitted[3]["projection"]["application_id"] == "ai_app"
