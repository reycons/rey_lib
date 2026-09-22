"""What code reaches a database object through, and where that resolves.

Three fact sets, published together and joined by value:

    code fact     a dispatch seam, a binding, and the symbol that names it.
                  INSTALLATION-INDEPENDENT, and it carries NO map -- measured:
                  at the Control seam the map is ctx.control.procedure_map,
                  which is installation configuration and not code.
    coverage      installation + seam -> the map that seam resolves through
                  here, plus what absence is allowed to mean.
    binding       installation + map + binding -> a routine, or SQL.

The join is then whole without any fact knowing more than it does:

    code symbol -> seam -> binding
                -> coverage(installation, seam) -> map
                -> binding(installation, map, binding) -> routine

**A seam is named, never discovered.** Only the dispatch points listed here are
read, and only literal arguments at them. That is the whole extraction: no
propagation, no caller analysis, no guessing. A binding a seam cannot resolve is
recorded as an observation and never as a fact.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.logs.logging_setup import get_logger

__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "EXTRACTOR_VERSION",
    "BridgeIndexWriter",
    "inspect_bridge",
]

logger = get_logger(__name__)

SCHEMA = "code"

#: What produced an extraction. Recorded on coverage, so a published bridge
#: says which extractor's reach it reflects.
EXTRACTOR_VERSION = "rey_repository_map/bridge 3"

#: WHAT THIS EXTRACTOR STATES, as a number a query can compare.
#:
#: Distinct from EXTRACTOR_VERSION, which is provenance for a human reading a
#: coverage row. This is the capability level db_binding_vw tests before it
#: asserts anything, so that an installation published by an older extractor
#: reads as NOT CHECKED rather than as checked and clean.
#:
#: Ordered, not a string match, so a reader asks "at least 2" rather than
#: knowing which literals mean what.
#:
#:   2  binding parameters and result_mode are published
#:   3  the dispatch method is published
BRIDGE_SCHEMA_VERSION = 3

#: The dispatch seams read, and nothing else. Each names the repository, the
#: module within it, the receiver and the methods whose FIRST argument is the
#: binding. Adding one is a decision, not a tweak.
_SEAMS = (
    {
        "seam": "control_dispatch",
        "repository": "rey_lib",
        "relative_path": "rey_lib/control/control.py",
        "receiver": "self",
        "methods": ("_call", "_call_rows"),
    },
)

_CODE_COLUMNS = (
    "repository_key", "relative_path", "qualified_name", "source_line",
    "seam", "observed_map", "observed_binding", "resolution",
    # The method the dispatch went through. _call_rows requires a
    # dataset_result binding; _call forbids one, because a dataset_result
    # binding leaves `outputs` empty and _call reads only that.
    "dispatch_method",
)
_BINDING_COLUMNS = (
    "installation", "map_name", "binding_name", "target_kind",
    "schema_name", "object_name", "signature",
    # What the binding says the routine gives back. One of the three things a
    # binding can be wrong about, and the one that fails loudest.
    "result_mode",
)
_BINDING_PARAMETER_COLUMNS = (
    "installation", "map_name", "binding_name", "parameter_name",
)
_COVERAGE_COLUMNS = (
    "installation", "seam", "map_name", "extractor_version", "status",
    # The capability level of this publication, which is what says whether the
    # assertions in db_binding_vw may run for this installation at all.
    "bridge_schema_version",
)

_STAGING = (
    "db_code_reference_stage",
    "db_binding_stage",
    "db_binding_parameter_stage",
    "db_bridge_coverage_stage",
)


def inspect_bridge(apps_root: Path, ctx: Any) -> dict[str, list[dict[str, Any]]]:
    """Everything the bridge index needs, for one installation.

    Args:
        apps_root: Directory holding the repository checkouts.
        ctx: The finalized installation context. Its procedure maps are the
            authority for what a binding resolves to; nothing here second
            guesses them.

    Returns:
        ``{"code": [...], "bindings": [...], "binding_parameters": [...],
        "coverage": [...]}``.
    """
    # Read from the object. The nested getattr this replaces existed only
    # because the shape was uncertain; an installation-backed context carries an
    # Installation, and a context without one names no installation at all.
    declared = getattr(ctx, "installation", None)
    installation = declared.name if declared is not None else ""
    control_map = str(
        getattr(getattr(ctx, "control", None), "procedure_map", "") or ""
    )

    code: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for seam in _SEAMS:
        source = apps_root / seam["repository"] / seam["relative_path"]
        observed, status = _seam_observations(source, seam)
        code.extend(observed)
        coverage.append({
            "installation": installation,
            "seam": seam["seam"],
            # Which map this seam resolves through HERE. The code cannot say;
            # the installation can.
            "map_name": control_map,
            "extractor_version": EXTRACTOR_VERSION,
            "status": status,
            "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        })

    bindings, binding_parameters = _bindings(ctx, installation)
    return {
        "code": code,
        "bindings": bindings,
        "binding_parameters": binding_parameters,
        "coverage": coverage,
    }


def _seam_observations(
    source: Path, seam: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Every dispatch at one seam, resolved or not.

    A non-literal first argument is recorded ``unresolved`` with what was seen,
    never dropped: silence would make "no code reaches this" indistinguishable
    from "the extractor could not tell".
    """
    if not source.exists():
        # Reported, not assumed empty. An unreadable seam is a coverage
        # failure, and publishing it as complete would launder it into "no
        # code reaches anything".
        logger.warning("bridge: seam source not found: %s", source)
        return [], "failed"

    tree = ast.parse(source.read_text(encoding="utf-8"))
    # DOTTED, not bare. The code index keys a symbol by
    # (repository_key, relative_path, qualified_name) and calls this method
    # 'Control.end_step'. A bare 'end_step' joins to nothing, which is how a
    # bridge fact becomes unreachable from the code it names.
    owners = [
        (node.lineno, node.end_lineno or node.lineno, dotted)
        for node, dotted in _qualified(tree)
    ]
    found: list[dict[str, Any]] = []
    complete = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in seam["methods"]):
            continue
        if not (isinstance(func.value, ast.Name)
                and func.value.id == seam["receiver"]):
            continue
        if not node.args:
            continue
        first = node.args[0]
        literal = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else ""
        )
        if not literal:
            complete = False
        owner = min(
            (o for o in owners if o[0] <= node.lineno <= o[1]),
            key=lambda o: node.lineno - o[0],
            default=(0, 0, "<module>"),
        )[2]
        found.append({
            "repository_key": seam["repository"],
            "relative_path": seam["relative_path"],
            "qualified_name": owner,
            "source_line": node.lineno,
            "seam": seam["seam"],
            # The map is not the code's to state at this seam.
            "observed_map": "",
            "observed_binding": literal or _seen(first),
            "resolution": "exact" if literal else "unresolved",
            # WHICH dispatch method reached it, which is what decides the
            # binding's result_mode. The seam has always filtered on this and
            # then thrown it away, so a binding could be read one way and
            # declared the other with nothing to notice.
            "dispatch_method": func.attr,
        })
    return found, ("complete" if complete else "partial")


def _qualified(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Every function in a module, with the dotted name the code index uses.

    A method is Class.method, a nested function Outer.inner, a module-level
    function its own name -- the same composition ``code.symbol.qualified_name``
    carries, so a bridge fact joins to the symbol it names.
    """
    found: list[tuple[ast.AST, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                dotted = f"{prefix}{child.name}"
                found.append((child, dotted))
                walk(child, f"{dotted}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _seen(node: ast.expr) -> str:
    """What stood where a binding was expected, for an unresolved observation."""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 -- the text is evidence, not behaviour
        return type(node).__name__


def _bindings(
    ctx: Any, installation: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every binding this installation's procedure maps declare, and its inputs.

    Derived from the authoritative YAML, which stays the source of truth. These
    rows are index state for querying and are never read back as configuration.

    Returns the bindings and their mapped parameters separately, because
    reporting WHICH parameter is wrong needs a row per parameter rather than a
    list packed into one.
    """
    made: list[dict[str, Any]] = []
    parameters: list[dict[str, Any]] = []
    for entry in list(getattr(ctx, "procedure_maps", None) or []):
        map_name = str(_get(entry, "name") or "")
        for binding in list(_get(entry, "routine_bindings") or []):
            routine = str(_get(binding, "routine") or "")
            schema_name, _, object_name = routine.partition(".")
            binding_name = str(_get(binding, "name") or "")
            made.append({
                "installation": installation,
                "map_name": map_name,
                "binding_name": binding_name,
                "target_kind": "routine",
                "schema_name": schema_name,
                "object_name": object_name,
                # Overloads are told apart by signature, and a map names none.
                # Resolution against the database index is by value and must
                # cope with that; inventing one here would be worse.
                "signature": "",
                "result_mode": str(_get(binding, "result_mode") or ""),
            })
            parameters.extend(
                {
                    "installation": installation,
                    "map_name": map_name,
                    "binding_name": binding_name,
                    "parameter_name": name,
                }
                for name in _bound_parameters(binding)
            )
        for binding in list(_get(entry, "sql_bindings") or []):
            made.append({
                "installation": installation,
                "map_name": map_name,
                "binding_name": str(_get(binding, "name") or ""),
                # SQL names no database object, and must not be given a
                # fabricated one.
                "target_kind": "sql",
                "schema_name": "", "object_name": "", "signature": "",
                "result_mode": str(_get(binding, "result_mode") or ""),
            })
    return made, parameters


def _bound_parameters(binding: Any) -> list[str]:
    """The routine parameters one binding maps, and only those.

    **``input`` only.** A binding's ``output`` block is
    ``{variable, load_to_ctx}`` -- both name a key in the run context, not a
    parameter of the routine. Reading them as parameters reports `variable` as
    an unknown parameter on almost every binding in the estate, which is noise
    that would bury the real findings.

    ``inputs`` is accepted as well as ``input`` because the map loader accepts
    both; an extractor that knew only the current spelling would silently
    publish no parameters for a binding written the legacy way, and a binding
    with no parameters passes every check.

    READ THROUGH ``keys()``, NOT BY ITERATING. A loaded binding's ``input`` is a
    ``config_namespace.Namespace``, not a dict: it has ``keys``, ``items`` and
    ``get``, but no ``__iter__``, and its ``__getitem__`` is attribute access --
    so iterating it raises on the first integer index. An ``isinstance(..., dict)``
    test looks right, passes a test written with dict literals, and publishes
    NOTHING against real configuration.
    """
    declared = _get(binding, "input")
    if declared is None:
        declared = _get(binding, "inputs")
    keys = getattr(declared, "keys", None)
    if not callable(keys):
        return []
    return [str(name) for name in keys()]


def _get(obj: Any, key: str) -> Any:
    """One field, however the config object carries it."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class BridgeIndexWriter:
    """Stages the bridge facts and promotes them in one call."""

    def __init__(self, adapter: DBAdapter, connection: Any) -> None:
        """Hold the adapter and the connection the bridge is published through."""
        self._adapter = adapter
        self._connection = connection

    def replace_index(self, inspected: dict[str, list[dict[str, Any]]]) -> None:
        """Replace the published bridge with this inspection.

        Args:
            inspected: What :func:`inspect_bridge` returned.

        Raises:
            DatabaseError: If staging or promotion fails, leaving the published
                bridge as it was -- the schema's guarantee, not this writer's.
        """
        self._clear_staging()
        self._stage("db_code_reference_stage", inspected["code"], _CODE_COLUMNS)
        self._stage("db_binding_stage", inspected["bindings"], _BINDING_COLUMNS)
        self._stage(
            "db_binding_parameter_stage",
            inspected.get("binding_parameters") or [],
            _BINDING_PARAMETER_COLUMNS,
        )
        self._stage(
            "db_bridge_coverage_stage", inspected["coverage"], _COVERAGE_COLUMNS,
        )
        self._call("p_publish_bridge")

    def _clear_staging(self) -> None:
        """Empty staging, so a run that staged and failed cannot be published."""
        for table in _STAGING:
            self._adapter.execute_sql(
                self._connection, f"DELETE FROM {SCHEMA}.{table}", {}, "no_return",
            )

    def _stage(
        self, table: str, rows: list[dict[str, Any]], columns: tuple[str, ...],
    ) -> None:
        """Insert staged rows. Empty is a legitimate result, not an error."""
        if not rows:
            return
        self._adapter.bulk_insert(
            self._connection, SCHEMA, table, rows, list(columns)
        )

    def _call(self, procedure: str) -> None:
        """Call the schema's promotion procedure."""
        self._adapter.execute_sql(
            self._connection, f"CALL {SCHEMA}.{procedure}()", {}, "no_return",
        )
