"""
YAML loading, parsing, validation, and config merge for rey_lib.

The single sanctioned home for ``import yaml`` in rey_lib: all YAML reading,
parsing, serialising, and validation live here, together with the deep-merge /
named-list merge behaviour and .env file loading. Split out of ``config_utils``
(SGC_Rey_Lib_Config_Utils_Responsibility_Split); YAML and merge behaviour are
unchanged.
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from rey_lib.config.config_namespace import Namespace
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

_CONFIG_DIR_NAME = "config"

_ENV_FILE_NAME   = ".env"

# Matches ${VAR_NAME} and $VAR_NAME patterns in YAML string values.
_ENV_VAR_PATTERN = re.compile(
    r"""
    \$\{([A-Z0-9_]+)\} |
    \$([A-Z0-9_]+)
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

def _yaml_files_in_folder(folder: Path, exclude: Path) -> list[Path]:
    """Return sorted YAML files for a folder or one YAML file path.

    ``folder`` may be either:
    - a directory, in which case all ``*.yaml`` files are returned recursively
    - a single ``.yaml`` file, in which case only that file is returned

    The root ``config.yaml`` is excluded so it is never merged twice.
    """
    if folder.is_file():
        if folder.suffix.lower() == ".yaml" and folder.resolve() != exclude:
            return [folder]
        return []

    return [
        f for f in sorted(folder.rglob("*.yaml"))
        if f.resolve() != exclude
    ]

def _find_parent_install_raw(config_path: Path) -> dict[str, Any] | None:
    """Walk parent directories to find a ``config.yaml`` with a ``paths:`` list."""
    current = config_path.parent.parent
    while True:
        candidate = current / "config.yaml"
        if candidate.exists() and candidate != config_path:
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "paths" in data:
                    return data
            except Exception:
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None

def _merge_compatible_collection(left: Any, right: Any, *, label: str) -> Any:
    if isinstance(left, list) and isinstance(right, list):
        return _merge_named_lists(left, right, label=label)
    if isinstance(left, dict) and isinstance(right, dict):
        return _merge_compatible_mapping(left, right, label=label)
    if left == right:
        return deepcopy(left)
    raise ConfigError(
        f"Conflicting compatibility aliases for '{label}': "
        f"values must use the same shape or match exactly."
    )

def _merge_named_lists(left: list[Any], right: list[Any], *, label: str) -> list[Any]:
    merged = deepcopy(left)
    name_to_index: dict[str, int] = {
        str(item["name"]): idx
        for idx, item in enumerate(merged)
        if isinstance(item, dict) and "name" in item
    }

    for item in right:
        if not isinstance(item, dict) or "name" not in item:
            if item not in merged:
                merged.append(deepcopy(item))
            continue

        name = str(item["name"])
        if name not in name_to_index:
            name_to_index[name] = len(merged)
            merged.append(deepcopy(item))
            continue

        existing = merged[name_to_index[name]]
        if existing != item:
            raise ConfigError(
                f"Conflicting duplicate '{label}' entry named '{name}'."
            )

    return merged

def _merge_compatible_mapping(left: Any, right: Any, *, label: str) -> Any:
    if not isinstance(left, dict) or not isinstance(right, dict):
        if left == right:
            return deepcopy(left)
        raise ConfigError(
            f"Conflicting compatibility aliases for '{label}': "
            "both values must be mappings."
        )

    merged = deepcopy(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = deepcopy(value)
            continue
        if merged[key] != value:
            raise ConfigError(
                f"Conflicting duplicate '{label}' entry named '{key}'."
            )
    return merged

def validate_yaml_file(path: str | Path) -> dict:
    """Validate a single YAML file for parse errors and unresolved env vars.

    Parameters
    ----------
    path : str | Path
        Path to the YAML file to validate.

    Returns
    -------
    dict
        Result with keys: status, checked_files, errors, warnings,
        env_refs, unresolved_env_refs.
        status is 'valid', 'warning', or 'invalid'.
    """
    path = Path(path)
    result = _new_validation_result()
    result["checked_files"].append(str(path))

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        _add_validation_issue(
            result,
            severity="error",
            file_path=path,
            message=f"Unable to read file: {exc}",
        )
        return result

    if not text.strip():
        _add_validation_issue(
            result,
            severity="warning",
            file_path=path,
            message="YAML file is empty.",
        )
        return result

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = column = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line   = exc.problem_mark.line + 1
            column = exc.problem_mark.column + 1
        _add_validation_issue(
            result,
            severity="error",
            file_path=path,
            message=str(exc),
            line=line,
            column=column,
        )
        return result

    env_refs = sorted(_find_yaml_env_refs(data))
    result["env_refs"] = env_refs

    for env_name in env_refs:
        if os.environ.get(env_name) is None:
            result["unresolved_env_refs"].append(env_name)
            _add_validation_issue(
                result,
                severity="warning",
                file_path=path,
                message=f"Unresolved environment variable: {env_name}",
            )

    return result

def validate_yaml_folder(path: str | Path) -> dict:
    """Validate all YAML files in a directory tree.

    Parameters
    ----------
    path : str | Path
        Directory to search recursively for *.yaml and *.yml files.

    Returns
    -------
    dict
        Aggregated result across all files. Same structure as
        validate_yaml_file.
    """
    path = Path(path)
    result = _new_validation_result()

    yaml_files = sorted(list(path.rglob("*.yaml")) + list(path.rglob("*.yml")))
    for yaml_file in yaml_files:
        file_result = validate_yaml_file(yaml_file)
        result["checked_files"].extend(file_result["checked_files"])
        result["errors"].extend(file_result["errors"])
        result["warnings"].extend(file_result["warnings"])
        result["env_refs"].extend(file_result["env_refs"])
        result["unresolved_env_refs"].extend(file_result["unresolved_env_refs"])

    result["env_refs"]            = sorted(set(result["env_refs"]))
    result["unresolved_env_refs"] = sorted(set(result["unresolved_env_refs"]))

    if result["errors"]:
        result["status"] = "invalid"
    elif result["warnings"]:
        result["status"] = "warning"

    return result


# ---------------------------------------------------------------------------
# Private — YAML validation helpers
# ---------------------------------------------------------------------------

def _new_validation_result() -> dict:
    return {
        "status": "valid",
        "checked_files": [],
        "errors": [],
        "warnings": [],
        "env_refs": [],
        "unresolved_env_refs": [],
    }

def _add_validation_issue(
    result: dict,
    *,
    severity: str,
    file_path: str | Path,
    message: str,
    line: int | None = None,
    column: int | None = None,
) -> None:
    issue = {
        "severity": severity,
        "file_path": str(file_path),
        "message": message,
        "line": line,
        "column": column,
    }
    if severity == "error":
        result["errors"].append(issue)
        result["status"] = "invalid"
    else:
        result["warnings"].append(issue)
        if result["status"] != "invalid":
            result["status"] = "warning"

def _find_yaml_env_refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found.update(_find_yaml_env_refs(v))
    elif isinstance(value, list):
        for v in value:
            found.update(_find_yaml_env_refs(v))
    elif isinstance(value, str):
        for match in _ENV_VAR_PATTERN.findall(value):
            env_name = match[0] or match[1]
            if env_name:
                found.add(env_name)
    return found

def _load_env_file(env_file: Path) -> None:
    """Load the .env file into os.environ if it exists."""
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)

def _load_yaml(path: Path) -> dict[str, Any]:
    """Validate then read a YAML file, returning an empty dict on blank files."""
    result = validate_yaml_file(path)
    if result["status"] == "invalid":
        messages = "; ".join(e["message"] for e in result["errors"])
        raise ConfigError(f"Invalid YAML file '{path}': {messages}")
    for w in result["warnings"]:
        _logger.warning("YAML validation warning in '%s': %s", path, w["message"])
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}

def parse_yaml(text: str) -> Any:
    """Parse YAML text — the sanctioned YAML parser.

    Application code must not import ``yaml`` directly. Read the file with
    rey_lib.files (``read_text_file``) and parse the resulting text here.
    Returns the parsed value (usually a dict) and ``{}`` for blank text;
    callers that require a mapping should validate the result themselves.

    Parameters
    ----------
    text : str
        YAML document text (e.g. from ``read_text_file`` or markdown frontmatter).

    Returns
    -------
    Any
        The parsed value (``{}`` when the text is blank).

    Raises
    ------
    ConfigError
        When the text is not valid YAML.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML text: {exc}") from exc
    return {} if data is None else data

def parse_yaml_namespace(text: str) -> "Namespace":
    """Parse YAML text into an attribute-access :class:`Namespace` context.

    Files are read with rey_lib.files; the context is built here. Non-mapping
    documents yield an empty context.

    Parameters
    ----------
    text : str
        YAML document text.

    Returns
    -------
    Namespace
        Attribute-access view over the parsed mapping.
    """
    data = parse_yaml(text)
    return Namespace(data if isinstance(data, dict) else {})

def dump_yaml(data: Any, *, sort_keys: bool = False) -> str:
    """Serialize a Python object to YAML text — the sanctioned YAML writer.

    Application code must not import ``yaml`` directly. Serialize here, then
    write the text with rey_lib.files. Uses block style and preserves key order
    and unicode by default.

    Parameters
    ----------
    data : Any
        Object to serialize (typically a dict).
    sort_keys : bool
        Whether to sort mapping keys. Default False (preserve insertion order).

    Returns
    -------
    str
        YAML document text.
    """
    return yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=sort_keys,
        allow_unicode=True,
    )

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override into base, returning a new dict.

    Nested dicts are merged recursively. Lists are concatenated with
    deduplication on named items (dicts with a 'name' key). All other
    values are overwritten by override.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = _merge_named_lists(result[key], value, label=key)
        elif key == "paths" and isinstance(result.get(key), list) and not isinstance(value, list):
            # PathResolver list must never be replaced by an app-level paths dict.
            pass
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Private — path resolution
# ---------------------------------------------------------------------------
