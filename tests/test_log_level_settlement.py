"""The level a process logs at is settled once, from four ordered terms.

    --log-level  >  the application's own declaration
                 >  the installation's log_level  >  ERROR

Each term is reachable only because the one before it can be ABSENT, so absence
is the thing these tests are really about. An operator's choice is recorded on
its own attribute rather than onto ``ctx.log_level``, because configuration may
write that too -- and one attribute carrying both would make the first term win
whenever either had spoken, leaving an application's declaration unreachable and
``applications.yaml`` decorative.

Settlement is also the only place the chain exists. ``setup_logging`` reads one
already-settled value and knows nothing about command lines, applications or
installations; the last test here is what keeps it that way.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from rey_lib.config.applications import Application
from rey_lib.config.bootstrap import DEFAULT_LOG_LEVEL, _settle_log_level
from rey_lib.config.cli import REQUESTED_LOG_LEVEL_ATTR, add_config_args
from rey_lib.errors.error_utils import ConfigError

APP = "fixture_app"


def _ctx(
    *,
    app_declares: Optional[str] = None,
    installation_declares: Optional[str] = None,
    requested: Optional[str] = None,
    app_name: str = APP,
) -> SimpleNamespace:
    """A context with each term of the chain explicitly present or absent.

    Every term is stated rather than left to a fixture's incidental shape: a
    test that passes because the fixture happened to omit a level is a test that
    changes meaning the day the fixture grows one.
    """
    ctx = SimpleNamespace(
        app_name=app_name,
        applications=(Application(name=APP, log_level=app_declares or ""),),
    )
    if installation_declares is not None:
        ctx.log_level = installation_declares
    if requested is not None:
        setattr(ctx, REQUESTED_LOG_LEVEL_ATTR, requested)
    return ctx


def _settled(ctx: Any) -> str:
    """Settle and return the level."""
    _settle_log_level(ctx)
    return ctx.log_level


class TestTheOperatorsChoiceIsPreservedAsAbsence:
    """The regression that the whole design turns on."""

    def test_an_omitted_argument_parses_as_none(self) -> None:
        """Absence has to survive the parser to be a term at all.

        A default of INFO here -- or of anything -- would make the first term of
        the chain always present, and no later term could ever be read.
        """
        parser = argparse.ArgumentParser()
        add_config_args(parser)

        assert parser.parse_args([]).log_level is None

    def test_an_omitted_argument_writes_nothing_and_the_application_wins(self) -> None:
        """Omission leaves the attribute unset, so the declaration is reached.

        Asserting the resolved value alone would pass even if the operator term
        always won, so this asserts the absence first and the consequence
        second.
        """
        ctx = _ctx(app_declares="DEBUG", installation_declares="INFO")

        assert not hasattr(ctx, REQUESTED_LOG_LEVEL_ATTR)
        assert _settled(ctx) == "DEBUG"


class TestThePrecedenceChain:
    """Each term, and each term being beaten by the one above it."""

    def test_nothing_declared_anywhere_is_the_default(self) -> None:
        """Both absences stated explicitly, not inherited from a fixture."""
        ctx = _ctx(app_declares=None, installation_declares=None)

        assert not hasattr(ctx, "log_level")
        assert _settled(ctx) == DEFAULT_LOG_LEVEL == "ERROR"

    def test_the_installation_is_used_when_the_application_is_silent(self) -> None:
        """An application that declares nothing inherits, silently and normally."""
        assert _settled(_ctx(app_declares=None, installation_declares="INFO")) == "INFO"

    def test_the_application_beats_the_installation(self) -> None:
        """Application policy belongs with application configuration."""
        assert _settled(_ctx(app_declares="DEBUG", installation_declares="INFO")) == "DEBUG"

    def test_the_operator_beats_the_application(self) -> None:
        """The command line is the highest term, and the only per-run one."""
        ctx = _ctx(app_declares="DEBUG", installation_declares="INFO", requested="ERROR")

        assert _settled(ctx) == "ERROR"


class TestTheVocabulary:
    """Four levels, matched to what the estate actually emits."""

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
    def test_every_native_level_is_selectable(self, level: str) -> None:
        """WARNING included: the estate has deliberate warning sites, and an
        operator who cannot select a level the code emits is a restriction
        needing its own reason."""
        assert _settled(_ctx(app_declares=level)) == level

    @pytest.mark.parametrize(
        ("declared", "expected"),
        [("debug", "DEBUG"), ("Info", "INFO"), ("wArNiNg", "WARNING")],
    )
    def test_case_is_normalized_at_the_boundary(self, declared: str, expected: str) -> None:
        """YAML, a command line and an installation may disagree on case only."""
        assert _settled(_ctx(app_declares=declared)) == expected

    def test_an_unknown_name_is_refused_and_names_its_source(self) -> None:
        """A typo must fail rather than quietly record the wrong amount.

        setup_logging maps a name it does not know to a default silently, which
        is exactly the run that looks normal and is not.
        """
        with pytest.raises(ConfigError, match="verbose"):
            _settle_log_level(_ctx(app_declares="verbose"))

    def test_the_refusal_says_which_term_declared_it(self) -> None:
        """Three sources can supply a level; the message says which one did."""
        with pytest.raises(ConfigError, match="--log-level"):
            _settle_log_level(_ctx(requested="chatty"))


class TestABrokenContextIsNotReadAsInheritance:
    """An application that cannot find itself is broken, not silent."""

    def test_an_unresolvable_app_name_raises(self) -> None:
        """Treating this as "no declaration, therefore inherit" would hide it
        behind a plausible level."""
        ctx = _ctx(app_name="not_the_fixture_app")

        with pytest.raises(ConfigError, match="not one of the applications"):
            _settle_log_level(ctx)

    def test_a_context_carrying_no_applications_inherits(self) -> None:
        """The ordinary bare context, which is absence rather than breakage."""
        ctx = SimpleNamespace(app_name=APP, applications=())

        assert _settled(ctx) == DEFAULT_LOG_LEVEL


class TestLoggingConsumesTheSettledValueOnly:
    """The architectural boundary, protected rather than a value.

    This is what stops precedence logic being reintroduced into the logging
    layer later: setup_logging is handed a context with nothing to resolve
    from, and must still log at the settled level.
    """

    def test_logging_consults_neither_applications_nor_cli_state(self) -> None:
        """The logging layer must not read either source of the chain.

        Asserted against the module's source rather than by calling
        setup_logging, which needs a fuller runtime context -- run identity,
        timestamps, a run store -- none of which bears on this property. A
        fixture built to satisfy all that would drift, and its failures would
        say nothing about the boundary.

        What is actually protected: the day someone resolves precedence here
        instead of at settlement, they must name one of these to do it.
        """
        import inspect

        from rey_lib.logs import logging_setup

        source = inspect.getsource(logging_setup)

        assert "applications" not in source, (
            "logging_setup reads ctx.applications. Precedence belongs to "
            "_settle_log_level; logging consumes one settled value."
        )
        assert REQUESTED_LOG_LEVEL_ATTR not in source, (
            f"logging_setup reads {REQUESTED_LOG_LEVEL_ATTR}. The operator's "
            "choice is ordered at settlement, not re-read here."
        )

    def test_settlement_leaves_one_value_for_logging_to_read(self) -> None:
        """Whatever the chain decided arrives as a plain ctx.log_level."""
        ctx = _ctx(app_declares="debug", installation_declares="INFO")

        _settle_log_level(ctx)

        assert ctx.log_level == "DEBUG"
        assert isinstance(ctx.log_level, str)
