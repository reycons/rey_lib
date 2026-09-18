"""The one authority that produces a completed context.

Every finished ``ctx`` in the estate is built here. Input sources supply the
plain state a context is assembled from -- installation YAML today, transported
pipeline state next -- and none of them assembles a context itself.

That rule exists because it was broken. ``load_ctx_snapshot`` built a second
context independently and the two drifted: it constructed its own
``PathResolver`` and ran none of the rest, so a pipeline child received
``ctx.applications`` as repr strings rather than ``Application`` objects and
nothing noticed, because nothing compared the two builders.

The sequence owned here, in order:

    normalize raw state -> wrap as Namespace -> PathResolver -> applications
    -> identity and logging defaults -> provenance -> completed context
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from rey_lib.config.applications import build_applications
from rey_lib.config.config_loader import _merge_compatible_collection
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import (
    PathResolver,
    _apply_path_resolver,
    _build_path_resolver,
    _resolve_paths,
)
from rey_lib.config.env_reference import ENV_REFERENCE_PREFIX, declaration_map
from rey_lib.config.provenance import ConfigMetadata
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)


class ContextBuilder:
    """Assemble one completed context from the plain state a source supplied.

    Construction takes settled inputs and :meth:`build` runs the sequence once.
    A source decides where the raw state came from; it does not decide what a
    finished context is.
    """

    def __init__(
        self,
        raw: dict[str, Any],
        *,
        config_dir: Path,
        config_path: Path,
        runtime_path_tokens: dict[str, str],
        metadata: ConfigMetadata | None = None,
        app_name: str | None = None,
        resolved_paths: dict[str, str] | None = None,
    ) -> None:
        """Hold the inputs one context is assembled from.

        Args:
            raw: The merged configuration state, already sourced and plain.
            config_dir: The directory relative file references resolve against.
            config_path: The installation root config file, recorded on the ctx.
            runtime_path_tokens: Date tokens a path template may reference.
            metadata: Provenance recorded while the state was sourced. A source
                that performed no merge has none to record, and passes None.
            app_name: The requesting app's identity, recorded when supplied.
            resolved_paths: Already-resolved ``name -> path`` state, supplied by
                a source that transports resolved values rather than the
                declared ``paths:`` list. The resolver is built here either way;
                this only says which form the path state arrived in.
        """
        self._raw = raw
        self._config_dir = config_dir
        self._config_path = config_path
        self._runtime_path_tokens = runtime_path_tokens
        self._metadata = metadata
        self._app_name = app_name
        self._resolved_paths = resolved_paths

    def build(self) -> Namespace:
        """Return the completed context.

        Returns:
            The fully populated ``Namespace``, carrying a resolved ``paths``
            resolver, built ``applications``, its identity and its provenance.

        Raises:
            ConfigError: If the configuration state is not assemblable -- an
                undeclared env reference, a retired section, or a collection
                written in a form that is no longer canonical.
        """
        raw = _apply_compatibility_aliases(self._raw)
        raw = _assemble_ctx_data(raw, self._config_dir)
        ctx = Namespace(raw)

        self._resolve_paths_onto(ctx)

        # Applications last of the collections: a parameter may resolve its
        # choices from workflows, pipelines or tools, and those are on ctx by
        # now. Built here rather than read later so the resolution happens once
        # -- the declaration under `apps` is the input and is never written to.
        object.__setattr__(ctx, "applications", build_applications(ctx))

        object.__setattr__(ctx, "config_path", str(self._config_path))
        if self._app_name:
            object.__setattr__(ctx, "app_name", self._app_name)
        # The declared level survives. This wrote "INFO" unconditionally, after
        # the configuration had been read -- so an installation that declared
        # `log_level: DEBUG` had it destroyed here before anything could act on
        # it, and the level was settable nowhere. The default applies only where
        # the configuration states nothing.
        if getattr(ctx, "log_level", None) is None:
            object.__setattr__(ctx, "log_level", "INFO")
        object.__setattr__(ctx, "log_depth", 0)
        # Provenance is stored separately under a private attribute so it never
        # appears in ctx.keys() and never shadows a real config value. A source
        # that merged nothing has no provenance to attach, and attaching an
        # empty record would claim the configuration came from nowhere rather
        # than that this context never read it.
        if self._metadata is not None:
            object.__setattr__(ctx, "_config_metadata", self._metadata)

        _logger.info(
            "config_loader complete top_level_keys=%s",
            [k for k in ctx.keys() if not k.startswith("_")],
        )
        return ctx

    def _resolve_paths_onto(self, ctx: Namespace) -> None:
        """Replace the path state with the one resolver built from it.

        The resolver is built once and exposed as ``ctx.paths``; every logical
        ``{name}`` reference elsewhere on the context is substituted from it, so
        there is one resolution and nothing left to resolve a second way. Which
        form the path state arrived in decides how the resolver is built and
        nothing else -- that is the whole difference between the sources.

        Args:
            ctx: The wrapped context, mutated in place.
        """
        path_resolver = self._path_resolver(ctx)
        if path_resolver is None:
            return

        object.__setattr__(ctx, "paths", path_resolver)
        _apply_path_resolver(ctx, path_resolver)

        if self._metadata is None:
            return

        # Record final resolved values for provenance (runtime values unchanged).
        resolver_strs = dict(self._runtime_path_tokens)
        resolver_strs.update({
            name: str(resolved) for name, resolved in path_resolver._paths.items()
        })
        self._metadata.resolve_values(resolver_strs)
        for name, resolved in path_resolver._paths.items():
            self._metadata.set_resolved(f"paths.{name}", str(resolved))

    def _path_resolver(self, ctx: Namespace) -> PathResolver | None:
        """Return the resolver for this context, or None when there is no path state.

        Args:
            ctx: The wrapped context, read for its declared ``paths`` list.

        Returns:
            The built resolver, or None when the configuration declared no
            canonical ``paths:`` list and nothing was transported either.
        """
        if self._resolved_paths is not None:
            # Already-resolved state: the values are the answer, so there is
            # nothing to expand and no token to expand it with.
            return PathResolver({
                name: Path(value) for name, value in self._resolved_paths.items()
            })

        declared = getattr(ctx, "paths", None)
        if not isinstance(declared, list):
            return None
        return _build_path_resolver(declared, self._runtime_path_tokens)


def _apply_compatibility_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply the remaining supported compatibility aliases.

    Database and LLM aliases remain structural bridges. Pipelines have one
    canonical top-level list and reject the retired nested representation.
    """
    result = deepcopy(raw)

    _alias_named_collection(
        result,
        current_key="db_connections",
        canonical_key="connections",
    )
    _alias_named_collection(
        result,
        current_key="llm_profiles",
        canonical_key="llm",
    )
    pipeline_coordinator = result.get("pipeline_coordinator")
    if (
        isinstance(pipeline_coordinator, dict)
        and "pipelines" in pipeline_coordinator
    ):
        raise ConfigError(
            "Config section 'pipeline_coordinator.pipelines' is retired; "
            "declare the canonical top-level 'pipelines' list instead."
        )
    pipelines = result.get("pipelines")
    if pipelines is not None and not isinstance(pipelines, list):
        raise ConfigError("Config section 'pipelines' must be a canonical list.")

    return result

def _alias_named_collection(
    raw: dict[str, Any],
    *,
    current_key: str,
    canonical_key: str,
) -> None:
    current_exists = current_key in raw
    canonical_exists = canonical_key in raw

    if current_exists and canonical_exists:
        merged = _merge_compatible_collection(
            raw[current_key],
            raw[canonical_key],
            label=canonical_key,
        )
        raw[current_key] = deepcopy(merged)
        raw[canonical_key] = deepcopy(merged)
    elif current_exists:
        raw[canonical_key] = deepcopy(raw[current_key])
    elif canonical_exists:
        raw[current_key] = deepcopy(raw[canonical_key])

def _assemble_ctx_data(raw: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Apply the non-file transformations needed before Namespace wrapping."""
    # Checked first, against what the author wrote: the nested form names its
    # variable directly and has nothing to declare, so validating after the
    # rewrite would demand a declaration for it.
    raw = _check_env_references(raw)
    raw = _declare_env_references(raw)
    raw = _resolve_paths(raw, config_dir, parent_key="")
    return raw

def _check_env_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Check that every ``env.<name>`` reference names a declared entry.

    The reference itself is left exactly as written. Nothing here reads the
    environment: a value backed by an environment variable is resolved by the
    subsystem that uses it, at the moment it is used, so the finalized context
    holds the reference and never the value.

    That is what makes the context safe to serialize, log or hand to a caller:
    there is nothing resolved in it to expose. It also means a variable changed
    after startup is seen by the next consumer that asks for it.

    An undeclared reference is still a configuration error, exactly as before --
    that is a mistake in the configuration and has nothing to do with whether
    the variable is set.
    """
    env_map = _build_env_reference_map(raw)
    if not env_map:
        return raw
    _assert_env_references_declared(raw, env_map, is_root=True)
    return raw

def _build_env_reference_map(raw: dict[str, Any]) -> dict[str, str]:
    """Build key_name -> env_var map from top-level env config entries.

    Through the same reader the resolver uses, so a reference that validates
    here is one that resolves later, and a declaration cannot be understood two
    ways on either side of the build.
    """
    return declaration_map(raw.get("env", []))

def _assert_env_references_declared(
    value: Any,
    env_map: dict[str, str],
    *,
    is_root: bool = False,
) -> None:
    """Walk the raw configuration and refuse a reference nobody declared."""
    if isinstance(value, dict):
        for key, child in value.items():
            # The declaration block names the references; it is not one.
            if is_root and key == "env":
                continue
            _assert_env_references_declared(child, env_map, is_root=False)
        return

    if isinstance(value, list):
        for item in value:
            _assert_env_references_declared(item, env_map, is_root=False)
        return

    if isinstance(value, str) and value.startswith(ENV_REFERENCE_PREFIX):
        name = value[len(ENV_REFERENCE_PREFIX):]
        if name not in env_map:
            raise ConfigError(
                f"Unknown env reference '{value}' — no matching key name in top-level env block."
            )


def _declare_env_references(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn the nested ``env:`` mapping form into ordinary symbolic references.

    Two spellings exist in configuration. A value may be written directly::

        password: env.REY_APPS_PASSWORD

    or the containing block may carry a map of target attribute to variable::

        env:
          password: REY_APPS_PASSWORD

    Both mean the same thing, so both end up as the same symbolic string in the
    finalized context. Rewriting the second form here, before the context is
    built, is what keeps them uniform -- and what stops the context being
    modified after construction.

    The two forms name their variable differently, though. The direct form names
    a *declared entry*, which the top-level ``env`` block maps to a variable::

        env:
          - name: openai_api_key
            env_var: FIXTURE_OPENAI_API_KEY

    while the nested form names the variable itself. So the nested form's
    variable is declared here as well, under its own name. That leaves one rule
    for whoever resolves these later: every ``env.<name>`` is looked up in the
    declaration block, and there is no second way a reference can be read.
    """
    declared: dict[str, str] = {}
    result = _rewrite_env_blocks(raw, declared)
    if declared:
        _add_declarations(result, declared)
    return result


def _rewrite_env_blocks(raw: Any, declared: dict[str, str]) -> Any:
    """Rewrite nested ``env:`` maps, recording the variables they name."""
    if isinstance(raw, dict):
        block = raw.get("env")
        result = {key: _rewrite_env_blocks(child, declared) for key, child in raw.items()}
        # A list under `env` is the declaration block, which names references
        # rather than assigning them.
        if isinstance(block, dict):
            for attr, env_var in block.items():
                name = str(env_var).strip()
                if name:
                    result[attr] = f"{ENV_REFERENCE_PREFIX}{name}"
                    declared[name] = name
        return result

    if isinstance(raw, list):
        return [_rewrite_env_blocks(item, declared) for item in raw]

    return raw


def _add_declarations(raw: dict[str, Any], declared: dict[str, str]) -> None:
    """Add declarations for nested-form variables that have none yet."""
    entries = raw.get("env")
    if not isinstance(entries, list):
        # No declaration block, or the root itself uses the nested form. Either
        # way there is nothing written here to preserve.
        entries = []
    known = {
        str(entry.get("name", "")).strip()
        for entry in entries
        if isinstance(entry, dict)
    }
    raw["env"] = [
        *entries,
        *(
            {"name": name, "env_var": env_var, "generate": False}
            for name, env_var in declared.items()
            if name not in known
        ),
    ]
