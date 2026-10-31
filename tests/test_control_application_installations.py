"""The #1297 database seam carries app-owned settings, never ctx configuration."""

from rey_lib.control.control import Control


def test_settings_read_is_scoped_to_the_requested_installation_and_app():
    control = Control.__new__(Control)
    calls = []
    settings = {"server": {"port": 8765}, "execution": {"log_record_limit": 250}}
    rows = [{"installation_id": 4, "application": "rey_bff", "settings": settings}]

    def answer(action, variables, required=False):
        calls.append((action, variables, required))
        return rows

    control._call_rows = answer

    assert control.get_application_installations(4, "rey_bff") == rows
    assert calls == [("get_application_installations", {
        "installation_id": 4, "application": "rey_bff",
    }, True)]
    assert not hasattr(control, "_ctx")


def test_no_memberships_is_not_replaced_with_discovered_packages_or_defaults():
    control = Control.__new__(Control)
    control._call_rows = lambda action, variables, required=False: []

    assert control.get_application_installations(5) == []


def test_governed_read_refusal_propagates_without_a_yaml_fallback():
    from rey_lib.errors.error_utils import AppError
    import pytest

    control = Control.__new__(Control)

    def refuse(action, variables, required=False):
        raise AppError("Application settings are unavailable")

    control._call_rows = refuse

    with pytest.raises(AppError, match="Application settings are unavailable"):
        control.get_application_installations(1, "rey_bff")
