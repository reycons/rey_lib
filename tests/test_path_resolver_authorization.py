"""PathResolver owns installation file-access authorization (backlog 1066).

security.allowed_roots names the locations file access may reach and configures
PathResolver; authorize() is the one check. Semantics are the Console's former
resolve_console_path / allowed_operation_roots, unchanged: the default set when
none is stated, the literal-location fallback for a name not configured, a
relative location taken beneath ``root``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import PathResolver, _build_path_resolver
from rey_lib.config.context_builder import _allowed_root_names
from rey_lib.errors.error_utils import ConfigError


@pytest.fixture
def estate(tmp_path: Path) -> dict[str, Path]:
    for name in ("root", "apps", "configs", "data", "logs", "state", "outside"):
        (tmp_path / name).mkdir()
    return {name: tmp_path / name for name in ("root", "apps", "configs", "data",
                                                "logs", "state")} | {"base": tmp_path}


def _resolver(estate: dict[str, Path], allowed: object = None) -> PathResolver:
    paths = {name: path for name, path in estate.items() if name != "base"}
    return PathResolver(paths, allowed)  # type: ignore[arg-type]


class TestAllowedRoots:
    def test_none_stated_is_the_default_set(self, estate: dict[str, Path]) -> None:
        roots = _resolver(estate).allowed_roots()

        assert roots == [estate[name].resolve() for name in
                         ("root", "apps", "configs", "data", "logs", "state")]

    def test_an_empty_list_is_the_default_set_too(self, estate: dict[str, Path]) -> None:
        assert _resolver(estate, []).allowed_roots() == _resolver(estate).allowed_roots()

    def test_stated_names_are_the_permitted_roots(self, estate: dict[str, Path]) -> None:
        assert _resolver(estate, ["data"]).allowed_roots() == [estate["data"].resolve()]

    def test_a_name_not_configured_is_a_literal_location(
        self, estate: dict[str, Path],
    ) -> None:
        literal = str(estate["base"] / "outside")

        assert _resolver(estate, [literal]).allowed_roots() == [Path(literal).resolve()]


class TestAuthorize:
    def test_a_location_beneath_a_permitted_root_is_authorized(
        self, estate: dict[str, Path],
    ) -> None:
        target = estate["data"] / "file.csv"

        assert _resolver(estate).authorize(str(target)) == target.resolve()

    def test_a_relative_location_is_taken_beneath_root(self, estate: dict[str, Path]) -> None:
        assert _resolver(estate).authorize("notes/a.txt") == (
            estate["root"] / "notes" / "a.txt").resolve()

    def test_a_location_outside_every_permitted_root_is_refused(
        self, estate: dict[str, Path],
    ) -> None:
        with pytest.raises(ConfigError, match="outside allowed roots"):
            _resolver(estate).authorize(str(estate["base"] / "outside" / "x"))

    def test_only_the_stated_roots_are_permitted(self, estate: dict[str, Path]) -> None:
        with pytest.raises(ConfigError, match="outside allowed roots"):
            _resolver(estate, ["data"]).authorize(str(estate["logs"] / "run.log"))

    def test_an_escape_by_dot_dot_is_refused(self, estate: dict[str, Path]) -> None:
        with pytest.raises(ConfigError, match="outside allowed roots"):
            _resolver(estate, ["data"]).authorize(str(estate["data"] / ".." / "outside"))

    def test_a_relative_location_without_root_is_refused(
        self, estate: dict[str, Path],
    ) -> None:
        resolver = PathResolver({"data": estate["data"]})

        with pytest.raises(ConfigError, match="Required path is not configured: root"):
            resolver.authorize("a.txt")


class TestAuthorizeBeneath:
    """Moved from the Console's file_explorer._authorized_path (backlog 1056)."""

    def test_a_location_beneath_its_root_is_authorized(self, estate: dict[str, Path]) -> None:
        (estate["data"] / "sub").mkdir()

        assert _resolver(estate).authorize_beneath(str(estate["data"]), "sub") == (
            estate["data"] / "sub").resolve()

    def test_the_root_itself_is_authorized_when_nothing_is_asked_beneath(
        self, estate: dict[str, Path],
    ) -> None:
        assert _resolver(estate).authorize_beneath(str(estate["data"]), "") == (
            estate["data"].resolve())

    def test_an_absolute_location_is_refused(self, estate: dict[str, Path]) -> None:
        with pytest.raises(ConfigError, match="relative to the exploration root"):
            _resolver(estate).authorize_beneath(str(estate["data"]), str(estate["logs"]))

    def test_leaving_the_root_is_refused(self, estate: dict[str, Path]) -> None:
        with pytest.raises(ConfigError, match="outside the exploration root: ../logs"):
            _resolver(estate).authorize_beneath(str(estate["data"]), "../logs")

    def test_a_contained_location_outside_the_allowed_roots_is_refused(
        self, estate: dict[str, Path],
    ) -> None:
        with pytest.raises(ConfigError, match="outside allowed roots"):
            _resolver(estate, ["data"]).authorize_beneath(str(estate["logs"]), "")


class TestWithPath:
    def test_adding_a_path_keeps_the_permitted_roots(self, estate: dict[str, Path]) -> None:
        resolver = _resolver(estate, ["data"])

        added = resolver.with_path("shared_venv", estate["base"] / "venv")

        assert added.resolve("shared_venv") == estate["base"] / "venv"
        assert added.allowed_roots() == [estate["data"].resolve()]
        assert resolver.resolve("data") == added.resolve("data")


class TestConstruction:
    def test_the_builder_carries_the_permitted_names(self, estate: dict[str, Path]) -> None:
        declared = [{"name": "data", "path": str(estate["data"])}]

        resolver = _build_path_resolver(declared, None, ["data"])

        assert resolver.allowed_roots() == [estate["data"].resolve()]

    @pytest.mark.parametrize("security, expected", [
        (None, ()),
        ({"allowed_roots": ["data", "logs"]}, ("data", "logs")),
        (Namespace({"allowed_roots": ["apps"]}), ("apps",)),
        ({"env_file": "x"}, ()),
    ])
    def test_the_security_block_is_read_as_the_console_read_it(
        self, security: object, expected: tuple,
    ) -> None:
        ctx = Namespace({}) if security is None else Namespace({"security": security})

        assert _allowed_root_names(ctx) == expected
