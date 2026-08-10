"""Phase 5B contract for governed LLM profile consumption."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest

from rey_lib.llm.exceptions import (
    ConfigurationFailure,
    PermissionFailure,
    ValidationFailure,
)
from rey_lib.llm.profiles import profile_access_policy, resolve_profile_for_llm


def _profile(
    *, allowed: list[str] | None = None, default: str = "redacted"
) -> SimpleNamespace:
    policy = None if allowed is None else SimpleNamespace(allowed=allowed, default=default)
    return SimpleNamespace(name="profile-1", provider="ignored", profile_access=policy)


def _ctx(profile: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(llm_profiles=[profile or _profile(allowed=["redacted"])])


def _current_state() -> dict:
    return {
        "status": "profile_available",
        "object_id": "file-1",
        "record": {
            "header": {"object_id": "file-1", "source_hash": "hash-1"},
            "structure": {
                "header_definition": {
                    "row_number": 1,
                    "columns": ["Customer Name"],
                },
                "distribution": {},
                "columns": [{"name": "Customer Name", "type": "text"}],
                "redacted_samples": [
                    {
                        "column": "Customer Name",
                        "sample_values": [{"value": "TOKEN", "count": 5}],
                    }
                ],
                "samples": [
                    {
                        "column": "Customer Name",
                        "sample_values": [{"value": "Alice", "count": 5}],
                    }
                ],
            },
        },
    }


def test_policy_projection_uses_only_declared_allowed_and_default() -> None:
    assert profile_access_policy(
        _profile(allowed=["redacted", "unredacted"], default="unredacted")
    ) == {"allowed": ["redacted", "unredacted"], "default": "unredacted"}


@pytest.mark.parametrize(
    "profile",
    [
        _profile(),
        _profile(allowed=["raw"]),
        _profile(allowed=["redacted"], default="unredacted"),
    ],
)
def test_missing_or_invalid_policy_fails_closed(profile: SimpleNamespace) -> None:
    with pytest.raises(ConfigurationFailure):
        profile_access_policy(profile)


def test_configured_default_selects_redacted_profile() -> None:
    with patch(
        "rey_lib.llm.profiles.lookup_profile_record", return_value=_current_state()
    ) as lookup:
        resolved = resolve_profile_for_llm(
            _ctx(), "profile-1", "file-1", "hash-1"
        )

    assert resolved["structure"]["redacted_samples"] == [
        {
            "column": "Customer Name",
            "sample_values": [{"value": "TOKEN", "count": 5}],
        }
    ]
    assert "samples" not in resolved["structure"]
    lookup.assert_called_once_with(ANY, "file-1", "hash-1")


def test_authorized_unredacted_selection_is_explicit() -> None:
    ctx = _ctx(_profile(allowed=["redacted", "unredacted"], default="redacted"))
    with patch(
        "rey_lib.llm.profiles.lookup_profile_record", return_value=_current_state()
    ):
        resolved = resolve_profile_for_llm(
            ctx,
            "profile-1",
            "file-1",
            "hash-1",
            profile_access="unredacted",
        )
    assert resolved["structure"]["samples"] == [
        {
            "column": "Customer Name",
            "sample_values": [{"value": "Alice", "count": 5}],
        }
    ]
    assert "redacted_samples" not in resolved["structure"]


def test_disallowed_session_selection_is_rejected_before_lookup() -> None:
    with patch("rey_lib.llm.profiles.lookup_profile_record") as lookup:
        with pytest.raises(PermissionFailure, match="does not allow"):
            resolve_profile_for_llm(
                _ctx(),
                "profile-1",
                "file-1",
                "hash-1",
                profile_access="unredacted",
            )
    lookup.assert_not_called()


@pytest.mark.parametrize("status", ["profile_missing", "profile_stale"])
def test_missing_and_stale_profiles_are_unavailable(status: str) -> None:
    state = {"status": status, "object_id": "file-1", "record": None}
    with patch("rey_lib.llm.profiles.lookup_profile_record", return_value=state):
        assert resolve_profile_for_llm(
            _ctx(), "profile-1", "file-1", "hash-1"
        ) is None


def test_available_record_never_falls_back_between_presentations() -> None:
    state = {
        "status": "profile_available",
        "object_id": "file-1",
        "record": {"structure": {"samples": [{"secret": "Alice"}]}},
    }
    with patch("rey_lib.llm.profiles.lookup_profile_record", return_value=state):
        with pytest.raises(ValidationFailure, match="redacted_samples"):
            resolve_profile_for_llm(_ctx(), "profile-1", "file-1", "hash-1")
