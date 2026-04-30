# rey_lib

Shared reusable library used across all projects. Zero app-specific logic.

---

## Package structure

```
rey_lib/
    config/
        config_utils.py     # YAML config loader, Namespace builder, inject_secrets
        ctx.py              # Generic context lookup helpers (find_by_name, etc.)
    logs/
        log_utils.py        # Centralised logging, call-depth indentation
    errors/
        error_utils.py      # AppError base, validators, handle_exception
    ftp/
        ftp_client.py       # FTP/FTPS/SFTP session, file listing, download
        state_manager.py    # State tracking, retry queue, stamp, failed files
        sync_engine.py      # Full sync orchestration per connection
    db/
        duckdb_utils.py     # DuckDB connection and SQL execution
    files/
        file_utils.py       # CSV / XLSX reading and writing
```

---

## Installation

```bash
pip install git+https://github.com/reycons/rey_lib.git@v0.7.0
```

In `requirements.txt`:

```
rey_lib @ git+https://github.com/reycons/rey_lib.git@v0.7.0
```

With optional dependencies:

```bash
# SFTP support
pip install "rey_lib[sftp] @ git+https://github.com/reycons/rey_lib.git@v0.7.0"

# DuckDB support
pip install "rey_lib[duckdb] @ git+https://github.com/reycons/rey_lib.git@v0.7.0"

# XLSX/pandas support
pip install "rey_lib[files] @ git+https://github.com/reycons/rey_lib.git@v0.7.0"
```

---

## Import paths

```python
# Config
from rey_lib.config.config_utils import build_ctx, inject_secrets, Namespace
from rey_lib.config.ctx import find_in_ctx, find_by_name

# Logging
from rey_lib.logs.log_utils import setup_logging, get_logger, log_enter, log_exit

# Errors
from rey_lib.errors.error_utils import AppError, ConfigError, handle_exception

# FTP
from rey_lib.ftp.ftp_client import ftp_session, list_remote_files, download_file
from rey_lib.ftp.state_manager import load_state, save_state, load_retry_queue
from rey_lib.ftp.sync_engine import run_sync

# Database
from rey_lib.db.duckdb_utils import init_db, get_connection, execute, fetch

# Files
from rey_lib.files.file_utils import get_reader, write_file, input_files
```

---

## Key design rules

- **No app-specific code** — nothing in this library knows about any project's tables, columns, or business rules.
- **Secrets are the caller's responsibility** — `build_ctx()` loads config. Call `inject_secrets(ctx, secret_map)` after with your own map.
- **App exceptions extend `AppError`** — define project-specific exceptions in your project extending `rey_lib.errors.error_utils.AppError`.
- **`duckdb_utils` requires explicit init** — call `init_db(db_path, sql_dir)` at startup.
- **`file_utils` filtering is the caller's responsibility** — pass a `row_filter` callable to `get_reader()`.
- **FTP protocol is config-driven** — set `ftp.protocol` to `ftp`, `ftps`, or `sftp` per connection.

---

## Project exception pattern

```python
# your_project/error_utils.py
from rey_lib.errors.error_utils import AppError

class MyProjectError(AppError): ...
class DataImportError(MyProjectError): ...
```

## Secret injection pattern

```python
from rey_lib.config.config_utils import build_ctx, inject_secrets

ctx = build_ctx(env=args.env, project_root=PROJECT_ROOT)
inject_secrets(ctx, {
    "ANTHROPIC_API_KEY": "llm.claude.api_key",
})
```

---

## Versioning

Tag releases before referencing from any project:

```bash
git tag v0.7.0
git push origin v0.7.0
```

Each project pins to a specific tag in `requirements.txt`.

---

## Version history

| Version | Changes |
|---------|---------|
| v0.7.0  | Restructured into sub-packages: config/, logs/, errors/, ftp/, db/, files/ |
| v0.6.0  | max_retry_sessions, abandon_to_failed_file, log_file reference in failed records |
| v0.5.0  | SFTP/FTPS support, directory glob expansion in remote_paths |
| v0.4.0  | Retry queue — failed downloads retried every run, never lost |
| v0.3.0  | ftp_client takes ftp_cfg Namespace, remove ctx from FTP signatures |
| v0.2.0  | sync_engine added, FTP exceptions added, pyproject.toml fixed |
| v0.1.0  | Initial release |
