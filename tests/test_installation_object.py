"""The installation, as an object, and the boundaries it must not cross.

The object exists because an identity with no object was re-derived by every
consumer and silently wrong in one: ``str()`` of the old namespace is truthy, so
a caller stringifying it got ``"namespace(name='local')"`` and never reached its
fallback.

Covers:
- the surface is identity only, and does not extend to paths
- a declared installation is strict about naming itself
- an installation-less context stays valid and nothing is synthesized
- the string form is the name, so the mistake is no longer expressible
- ctx.paths remains the single path authority
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.config_utils import PathResolver, build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError
from rey_lib.installation.installation import Installation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_config(root: Path, body: str) -> Path:
    """Write a minimal installation config and return its path."""
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    config = configs / "config.yaml"
    config.write_text(body, encoding="utf-8")
    return config


@pytest.fixture()
def installation_config(tmp_path: Path) -> Path:
    """An installation declaring a name and a type."""
    return _write_config(tmp_path, f"""\
installation:
  name: ccc
  type: explorer_only

paths:
  - name: root
    path: {tmp_path}

  - name: data
    path: "{{root}}/data"
""")


@pytest.fixture()
def installation_less_config(tmp_path: Path) -> Path:
    """A standalone CLI config, declaring no installation at all."""
    return _write_config(tmp_path, f"""\
app: standalone_tool

paths:
  - name: root
    path: {tmp_path}
""")


# ---------------------------------------------------------------------------
# The surface is identity only
# ---------------------------------------------------------------------------

class TestTheSurfaceIsIdentityOnly:
    """Identity, and deliberately nothing else."""

    def test_it_carries_name_and_type(self, installation_config: Path) -> None:
        """The declared identity reaches the object."""
        ctx = build_ctx_from_path(installation_config)

        assert type(ctx.installation) is Installation
        assert ctx.installation.name == "ccc"
        assert ctx.installation.type == "explorer_only"

    def test_an_undeclared_type_is_none_not_defaulted(self, tmp_path: Path) -> None:
        """Undeclared stays undeclared.

        What an absent type *means* is the consuming application's decision --
        the Console defaults it to standard -- so defaulting it here would put
        application vocabulary in rey_lib and give two answers.
        """
        config = _write_config(tmp_path, f"""\
installation:
  name: ccc

paths:
  - name: root
    path: {tmp_path}
""")

        assert build_ctx_from_path(config).installation.type is None

    def test_it_has_no_paths(self, installation_config: Path) -> None:
        """There is no second way to reach the path authority.

        An Installation.paths would be a wrapper over an object that is already
        an object, and two ways to resolve a path drift. Asserted rather than
        left to review, because it is the boundary most likely to be crossed by
        someone adding a convenience accessor.
        """
        installation = build_ctx_from_path(installation_config).installation

        assert not hasattr(installation, "paths")
        assert not hasattr(installation, "resolve_path")

    def test_ctx_paths_is_still_the_single_path_authority(
        self, installation_config: Path
    ) -> None:
        """The resolver is untouched, and the declared list did not survive."""
        ctx = build_ctx_from_path(installation_config)

        assert isinstance(ctx.paths, PathResolver)
        assert not isinstance(ctx.paths, list)
        assert ctx.paths.resolve("data") == (Path(ctx.paths.resolve("root")) / "data")


# ---------------------------------------------------------------------------
# Strict when declared, absent when not
# ---------------------------------------------------------------------------

class TestDeclaredOrAbsent:
    """Every installation-backed context has one; a CLI context may have none."""

    def test_an_installation_less_context_is_valid(
        self, installation_less_config: Path
    ) -> None:
        """A standalone CLI context builds, and carries no installation.

        The invariant is not that every ctx has an Installation. Synthesizing a
        default would attach an identity nobody declared to whatever the run
        goes on to record, which would look attributed and not be.
        """
        ctx = build_ctx_from_path(installation_less_config)

        assert getattr(ctx, "installation", None) is None

    def test_a_declared_installation_must_name_itself(self, tmp_path: Path) -> None:
        """Declared-but-nameless is a configuration error, not an absent one.

        Absent and broken are different states. The first is a CLI context; the
        second is an installation whose configuration forgot to say which.
        """
        config = _write_config(tmp_path, f"""\
installation:
  type: explorer_only

paths:
  - name: root
    path: {tmp_path}
""")

        with pytest.raises(ConfigError, match="declares no name"):
            build_ctx_from_path(config)

    def test_a_blank_name_is_refused(self) -> None:
        """Whitespace is not an identity."""
        with pytest.raises(ConfigError, match="declares no name"):
            Installation(name="   ")


# ---------------------------------------------------------------------------
# The mistake that prompted the object
# ---------------------------------------------------------------------------

class TestTheMistakeIsNoLongerExpressible:
    """Stringifying it gives the name, not a repr of a namespace."""

    def test_str_is_the_name(self, installation_config: Path) -> None:
        """The value every caller stringifying the old namespace wanted."""
        installation = build_ctx_from_path(installation_config).installation

        assert str(installation) == "ccc"
        assert "namespace" not in str(installation)

    def test_to_dict_is_the_identity_surface(self) -> None:
        """to_dict states what the object models, not what the YAML held."""
        assert Installation(name="ccc", type="explorer_only").to_dict() == {
            "name": "ccc",
            "type": "explorer_only",
        }
        assert Installation(name="ccc").to_dict() == {"name": "ccc", "type": None}
