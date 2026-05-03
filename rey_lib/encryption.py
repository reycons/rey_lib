"""Encryption and environment-key helpers.

This module centralizes Fernet key generation plus `.env` file update helpers.
It also provides a config-driven generator that reads `config/config.<env>.yaml`
entries under the top-level `env` block and generates missing keys only when
`generate: true`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rey_lib.errors.error_utils import ConfigError, validate_env

__all__ = [
    "generate_fernet_key",
    "ensure_env_key",
    "ensure_generated_env_keys",
]


def generate_fernet_key() -> str:
    """Generate and return a new Fernet key as a UTF-8 string."""
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigError(
            "The 'cryptography' package is required for encryption key generation."
        ) from exc

    return Fernet.generate_key().decode("utf-8")


def ensure_env_key(env_file: Path, env_var: str) -> bool:
    """Ensure env_var exists in env_file; generate and append if missing.

    Parameters
    ----------
    env_file : Path
        Target .env file path.
    env_var : str
        Environment variable name to ensure.

    Returns
    -------
    bool
        True when a new key was generated and written, False when the variable
        already existed and no changes were made.
    """
    existing_lines, existing_vars = _read_env_file(env_file)
    if env_var in existing_vars:
        return False

    new_line = f"{env_var}={generate_fernet_key()}\n"
    _write_env_file(env_file, existing_lines, [new_line])
    return True


def ensure_generated_env_keys(
    project_root: Path,
    env: str,
    env_file: Path | None = None,
) -> list[str]:
    """Generate keys for config env entries where generate=true and missing.

    Reads `config/config.<env>.yaml` and expects top-level entries like:

        env:
          - name: account_encryption_key
            env_var: ACCOUNT_ENCRYPTION_KEY
            generate: true

    Parameters
    ----------
    project_root : Path
        Project root directory containing config/.
    env : str
        Runtime environment (dev or prod).
    env_file : Path | None
        Optional .env file path; defaults to <project_root>/.env.

    Returns
    -------
    list[str]
        Environment variable names that were generated and written.
    """
    env = validate_env(env)
    project_root = Path(project_root).resolve()
    cfg_path = project_root / "config" / f"config.{env}.yaml"
    target_env_file = env_file.resolve() if env_file else project_root / ".env"

    config_data = _load_yaml(cfg_path)
    entries = config_data.get("env", [])

    generated: list[str] = []
    for entry in entries:
        entry_dict = _to_dict(entry)
        if not entry_dict:
            continue

        should_generate = bool(entry_dict.get("generate", False))
        env_var = str(entry_dict.get("env_var", "")).strip()

        if should_generate and env_var:
            if ensure_env_key(target_env_file, env_var):
                generated.append(env_var)

    return generated


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file, returning empty dict for blank files."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _to_dict(value: Any) -> dict[str, Any]:
    """Convert Namespace-like values to dict; return empty dict otherwise."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "items"):
        return {k: v for k, v in value.items()}
    return {}


def _read_env_file(env_file: Path) -> tuple[list[str], set[str]]:
    """Read env_file and return (raw_lines, declared_variable_names)."""
    if not env_file.exists():
        return [], set()

    lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
    names = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    return lines, names


def _write_env_file(
    env_file: Path,
    existing_lines: list[str],
    new_lines: list[str],
) -> None:
    """Write existing_lines + new_lines to env_file, preserving newline safety."""
    env_file.parent.mkdir(parents=True, exist_ok=True)

    content = "".join(existing_lines)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(new_lines)

    env_file.write_text(content, encoding="utf-8")
