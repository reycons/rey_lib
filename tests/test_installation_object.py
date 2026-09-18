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
- the registry id is attached once, and excluded from what the object *is*
- the bootstrap seam resolves exactly once, and only where there is something
  to resolve
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.bootstrap import _settle_installation_id
from rey_lib.config.config_namespace import Namespace
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
        assert Installation(
            name="ccc", type="explorer_only", installation_id=2,
        ).to_dict() == {
            "name": "ccc",
            "type": "explorer_only",
            "installation_id": 2,
        }
        assert Installation(name="ccc").to_dict() == {
            "name": "ccc",
            "type": None,
            "installation_id": None,
        }


# ---------------------------------------------------------------------------
# The resolved id
# ---------------------------------------------------------------------------

class TestTheResolvedId:
    """One transition, guarded, and excluded from what the object *is*."""

    def test_it_is_unresolved_at_construction(
        self, installation_config: Path
    ) -> None:
        """Configuration does not reach a database, so there is no id yet."""
        assert build_ctx_from_path(installation_config).installation.installation_id is None

    def test_attaching_the_same_id_twice_is_accepted(self) -> None:
        """A re-entered bootstrap or an adopting child is not an error."""
        installation = Installation(name="ccc")
        installation.attach_installation_id(7)
        installation.attach_installation_id(7)

        assert installation.installation_id == 7

    def test_attaching_a_different_id_raises(self) -> None:
        """Two answers to one identity is the defect, so neither is kept."""
        installation = Installation(name="ccc")
        installation.attach_installation_id(7)

        with pytest.raises(ConfigError, match="already attached"):
            installation.attach_installation_id(8)

    def test_the_id_is_not_part_of_what_the_installation_is(self) -> None:
        """Equality and hash read the declaration, not the registry.

        Two objects naming one installation are the same installation whether or
        not one has been to the database. Including the id would also move an
        object's hash when it resolved, which would strand it if it were already
        a key somewhere.
        """
        declared = Installation(name="ccc")
        before = hash(declared)
        declared.attach_installation_id(7)

        assert hash(declared) == before
        assert declared == Installation(name="ccc")


# ---------------------------------------------------------------------------
# The bootstrap seam
# ---------------------------------------------------------------------------

class _CountingControl:
    """A Control that records how many times it was asked to resolve."""

    def __init__(self, installation_id: int = 4) -> None:
        self.installation_id = installation_id
        self.calls: list[str] = []

    def resolve_installation(self, installation_key: str) -> int:
        """Answer, and remember having been asked."""
        self.calls.append(installation_key)
        return self.installation_id


class _SilentControl:
    """A Control whose binding returns nothing, as a misbound one would."""

    def resolve_installation(self, installation_key: str) -> None:
        """Return nothing at all."""
        return None


def _ctx(installation: Installation | None, control: object | None) -> Namespace:
    """A context carrying only what the seam reads."""
    ctx = Namespace({})
    object.__setattr__(ctx, "installation", installation)
    object.__setattr__(ctx, "shared_control", control)
    return ctx


class TestTheBootstrapSeam:
    """Resolve once, and only where there is something to resolve."""

    def test_it_resolves_and_attaches(self) -> None:
        """The ordinary case: installation-backed, control open, id unknown."""
        installation = Installation(name="ccc")
        control = _CountingControl(installation_id=4)

        _settle_installation_id(_ctx(installation, control))

        assert installation.installation_id == 4
        assert control.calls == ["ccc"]

    def test_an_already_known_id_is_not_resolved_again(self) -> None:
        """The invariant: one resolution per identity, not one per bootstrap.

        A child arrives carrying its parent's id. Attaching the same value would
        succeed, so a correct end state proves nothing -- the call count is what
        distinguishes inheriting an answer from asking the question twice.
        """
        installation = Installation(name="ccc", installation_id=4)
        control = _CountingControl(installation_id=4)

        _settle_installation_id(_ctx(installation, control))

        assert control.calls == []
        assert installation.installation_id == 4

    def test_an_installation_less_context_resolves_nothing(self) -> None:
        """A standalone CLI context does not acquire an installation here."""
        control = _CountingControl()

        _settle_installation_id(_ctx(None, control))

        assert control.calls == []

    def test_no_control_means_no_resolution_and_no_failure(self) -> None:
        """An installation with no control database still boots.

        The Console's own is this case. There is no registry to ask, which is an
        ordinary state rather than a reason to refuse.
        """
        installation = Installation(name="ccc")

        _settle_installation_id(_ctx(installation, None))

        assert installation.installation_id is None

    def test_a_binding_that_returns_nothing_is_reported(self) -> None:
        """A misbound resolver is a configuration fault, named as one.

        The routine raises on an unknown or blank key, so returning None means
        the binding names the wrong routine or reads no OUT parameter -- and a
        binding is never checked against the catalog.
        """
        installation = Installation(name="ccc")

        with pytest.raises(ConfigError, match="returned no id"):
            _settle_installation_id(_ctx(installation, _SilentControl()))
