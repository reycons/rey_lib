"""Canonical provider policy and read-only profile consumption for LLM callers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from rey_lib.config.ctx import find_in_ctx
from rey_lib.llm.exceptions import (
    ConfigurationFailure,
    PermissionFailure,
    ValidationFailure,
)
from rey_lib.logs.profile_library import lookup_profile_record

__all__ = [
    "PROFILE_ACCESS_REDACTED",
    "PROFILE_ACCESS_UNREDACTED",
    "profile_access_policy",
    "resolve_profile_for_llm",
]

PROFILE_ACCESS_REDACTED = "redacted"
PROFILE_ACCESS_UNREDACTED = "unredacted"
_ACCESS_SAMPLE_FIELDS = {
    PROFILE_ACCESS_REDACTED: "redacted_samples",
    PROFILE_ACCESS_UNREDACTED: "samples",
}


def profile_access_policy(profile: Any) -> dict[str, Any]:
    """Return one validated provider profile-access policy.

    The named execution profile's YAML declaration is the only authority. No
    provider name, endpoint, model, or other characteristic grants access.
    Missing and invalid declarations fail closed.
    """
    policy = _value(profile, "profile_access")
    allowed_value = _value(policy, "allowed")
    default_value = _value(policy, "default")
    if not isinstance(allowed_value, (list, tuple)) or not allowed_value:
        raise ConfigurationFailure(
            "LLM execution profile requires profile_access.allowed."
        )

    allowed: list[str] = []
    for value in allowed_value:
        mode = str(value or "").strip()
        if mode not in _ACCESS_SAMPLE_FIELDS:
            raise ConfigurationFailure(
                "profile_access.allowed may contain only redacted or unredacted."
            )
        if mode not in allowed:
            allowed.append(mode)

    default = str(default_value or "").strip()
    if default not in allowed:
        raise ConfigurationFailure(
            "profile_access.default must be a member of profile_access.allowed."
        )
    return {"allowed": allowed, "default": default}


def resolve_profile_for_llm(
    ctx: Any,
    execution_profile: str,
    object_id: str,
    source_hash: str,
    *,
    profile_access: str = "",
) -> dict[str, Any] | None:
    """Return the authorized current profile, or ``None`` when unavailable.

    Authorization is re-evaluated from ``ctx.llm_profiles`` on every call. An
    empty session selection uses the configured default. Missing and stale
    profile records are unavailable and never fall back to another presentation.
    """
    profile_name = str(execution_profile or "").strip()
    profile = find_in_ctx(ctx, "llm_profiles", profile_name)
    if profile is None:
        raise ConfigurationFailure(
            f"llm_execution_profile not found: {profile_name or '(none)'}"
        )
    policy = profile_access_policy(profile)
    selected = str(profile_access or "").strip() or str(policy["default"])
    if selected not in policy["allowed"]:
        raise PermissionFailure(
            f"LLM execution profile '{profile_name}' does not allow "
            f"profile_access '{selected}'."
        )

    state = lookup_profile_record(ctx, object_id, source_hash)
    if state.get("status") != "profile_available":
        return None

    record = state.get("record")
    if not isinstance(record, Mapping):
        raise ValidationFailure(
            f"Current profile record for object_id '{object_id}' is invalid."
        )
    resolved = deepcopy(dict(record))
    structure = resolved.get("structure")
    if not isinstance(structure, dict):
        raise ValidationFailure(
            f"Current profile record for object_id '{object_id}' has no valid structure."
        )
    selected_field = _ACCESS_SAMPLE_FIELDS[selected]
    rejected_field = (
        "samples" if selected_field == "redacted_samples" else "redacted_samples"
    )
    selected_samples = structure.get(selected_field)
    if not isinstance(selected_samples, list):
        raise ValidationFailure(
            f"Current profile record for object_id '{object_id}' has no valid "
            f"{selected_field}."
        )
    structure.pop(rejected_field, None)
    return resolved


def _value(item: Any, field: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(field)
    return getattr(item, field, None)
