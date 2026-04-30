# rey_lib

Shared reusable library used across all projects. Zero app-specific logic.

## Modules

| Module | Purpose |
|---|---|
| `config_utils` | YAML config loader, deep-merge, Namespace builder |
| `ctx` | Generic context lookup helpers (`find_by_name`, etc.) |
| `error_utils` | Generic base exception (`AppError`), input validators |
| `log_utils` | Centralised logging, call-depth indentation |
| `ftp_client` | FTP connection, file listing, file download |
| `state_manager` | JSON-backed file download state tracking |
| `duckdb_utils` | DuckDB connection and SQL execution |
| `file_utils` | CSV / XLSX reading and writing |

---

## Installation

```bash
pip install git+https://github.com/joerey/rey_lib.git@v0.1.0
```

Or in `requirements.txt`:

```
rey_lib @ git+https://github.com/joerey/rey_lib.git@v0.1.0
```

Optional dependencies:

```bash
pip install "rey_lib[duckdb] @ git+https://github.com/joerey/rey_lib.git@v0.1.0"
pip install "rey_lib[files] @ git+https://github.com/joerey/rey_lib.git@v0.1.0"
```

---

## Key design rules

- **No app-specific code** — nothing in this library knows about any project's tables, columns, file formats, or business rules.
- **Secrets are the caller's responsibility** — `build_ctx()` loads config and resolves paths. Call `inject_secrets(ctx, secret_map)` separately with your project's own map.
- **App exceptions extend `AppError`** — define project-specific exceptions in your project's `error_utils.py` by extending `rey_lib.error_utils.AppError`.
- **`duckdb_utils` requires explicit init** — call `init_db(db_path, sql_dir)` at startup. No paths are assumed.
- **`file_utils` filtering is the caller's responsibility** — pass a `row_filter` callable to `get_reader()`. This library never imports transformer or application logic.

---

## Project exception pattern

```python
# your_project/error_utils.py
from rey_lib.error_utils import AppError

class MyProjectError(AppError): ...
class DataImportError(MyProjectError): ...
class DatabaseError(MyProjectError): ...
```

## Secret injection pattern

```python
# main.py
from rey_lib.config_utils import build_ctx, inject_secrets

ctx = build_ctx(env=args.env)
inject_secrets(ctx, {
    "ANTHROPIC_API_KEY": "llm.claude.api_key",
    "DB_PASSWORD":       "database.password",
})
```

---

## Versioning

Tag releases before referencing from any project:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Each project pins to a specific tag in `requirements.txt`.
