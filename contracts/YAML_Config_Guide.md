# YAML Configuration File Guide

**Version:** 1.0  
**Applies to:** All Python projects governed by the LLM Programming Contract

---

## Overview

Every project uses a two-tier configuration system:

- **Main config files** — one per environment (`config.dev.yaml`, `config.prod.yaml`). Singleton values and global settings only. Never contains named instances (databases, data feeds, brokers, etc.).
- **Sub-config files** — one file per named instance. Discovered automatically by `config_utils.py` at startup. Adding a new instance requires only dropping in a new file — no code changes.

All config is assembled by `config_utils.py` into a single `ctx` (AppContext) object at startup. All subsequent code reads from `ctx` only — never from config files directly.

---

## File Naming Conventions

### Main Config Files

```
config.dev.yaml
config.prod.yaml
```

One pair per project, at the project root. Environment is selected at runtime via CLI argument — never hard-coded.

### Sub-Config Files

```
config.{section}.{name}.yaml
```

Examples:

```
config.db.SQLServer_NaviControl.yaml
config.db.SQLServer_NaviStage.yaml
config.data_source.advantage_trade.yaml
config.stages.ftp_sync.yaml
```

Rules:

- `{section}` — the logical category (e.g. `db`, `data_source`, `stages`, `broker`)
- `{name}` — a unique identifier for this instance within the section (e.g. `SQLServer_NaviControl`, `advantage_trade`)
- Use underscores within `{name}` — no spaces
- File name is the only registration mechanism — `config_utils.py` discovers files by scanning for the `config.{section}.{name}.yaml` pattern

---

## Main Config File Structure

The main config contains only singleton values — settings that have exactly one value per environment and do not vary by named instance.

### Template

```yaml
# =============================================================================
# {Project Name} — Main Configuration ({env})
#
# Singleton values and global settings only.
# Named instances live in sub-config files: config.{section}.{name}.yaml
#
# PATH RULES
#   Paths starting with ~   are relative to your home directory
#   All other paths         are treated as absolute
# =============================================================================

# -----------------------------------------------------------------------------
# Logging
# {operation} and {timestamp} are substituted at runtime.
# Each run produces a distinct log file.
# -----------------------------------------------------------------------------

log_path: /path/to/logs/{project}.{operation}.{timestamp}.log

# -----------------------------------------------------------------------------
# Data handling
# -----------------------------------------------------------------------------

chunk_size: 500
```

### Rules

- No named instance lists here — those go in sub-config files
- `log_path` uses `{operation}` and `{timestamp}` tokens substituted at runtime by `log_utils.py`
- `chunk_size` must always be present — it governs all chunked DB fetches and is read from `ctx.chunk_size`
- Dev and prod values differ where needed (log paths, chunk sizes, etc.)
- Do not add application-specific singleton values without updating `AppContext` in `ctx.py` first

### Dev vs Prod Differences

| Setting | Dev | Prod |
|---|---|---|
| `log_path` | `~/.{project}/logs/...` | Absolute path on server |
| `chunk_size` | Smaller value acceptable for testing | Production-tuned value |

---

## Sub-Config File Structure

Each sub-config file defines exactly one named instance within a section.

### General Template

```yaml
# =============================================================================
# {Section Label}: {Instance Description}
# =============================================================================

{section}:
  {key}: {value}
  {key}: {value}
```

The top-level key must match the section name used in the file name. `config_utils.py` uses this key to merge the sub-config into `ctx` under the correct namespace.

---

## Section Reference

### `db` — Database Connections

**File name:** `config.db.{provider}_{DatabaseName}.yaml`

**Examples:** `config.db.SQLServer_NaviControl.yaml`, `config.db.MySQL_Reporting.yaml`

```yaml
# =============================================================================
# Database Connection: {Provider} — {DatabaseName}
# =============================================================================

db:
  connections:
    - name:     {Provider}_{DatabaseName}_{host_alias}
      provider: SQLServer                    # SQLServer | MySQL | PostgreSQL
      database: {DatabaseName}
      host:     {hostname_or_ip}
      driver:   ODBC Driver 17 for SQL Server   # SQL Server only
      port:     1433
      # user and password injected from .env at startup via inject_secrets()
      # {PROVIDER}_{DBNAME}_USER / {PROVIDER}_{DBNAME}_PASSWORD
      # Omit user entirely to use Windows Authentication (SQL Server only)

  # Optional — designates which connection owns batch and step logging.
  # Include only in the db sub-config for the control/logging database.
  batch_connection: {Provider}_{DatabaseName}_{host_alias}
```

**Rules:**

- `name` must be unique across all db sub-configs in the project
- `user` and `password` are never in YAML — they are injected from `.env` at startup
- `batch_connection` appears in at most one db sub-config — the database that owns batch logging
- Omit `user` entirely (do not set it to blank) when using Windows Authentication
- One file per database — never combine two databases in one file

**Supported providers:**

| Provider | `provider` value | `driver` required |
|---|---|---|
| SQL Server | `SQLServer` | Yes — `ODBC Driver 17 for SQL Server` |
| MySQL | `MySQL` | No |
| PostgreSQL | `PostgreSQL` | No |

---

### `data_source` — External Data Feed Definitions

**File name:** `config.data_source.{name}.yaml`

**Example:** `config.data_source.advantage_trade.yaml`

```yaml
# =============================================================================
# Data Source: {Name}
# =============================================================================

data_source:
  name:         {unique_identifier}
  description:  {human readable description}
  # Add source-specific fields below — file paths, schemas, delimiters, etc.
  file_path:    /path/to/data/{name}/
  file_pattern: "*[DateString]*.csv"
  delimiter:    ","
```

**Rules:**

- `name` must match the `{name}` portion of the file name exactly
- All paths use the path rules from the main config (absolute or `~`-relative)
- No credentials here — any auth values are injected from `.env`

---

### `stages` — External Stage Runners

**File name:** `config.stages.{name}.yaml`

**Example:** `config.stages.ftp_sync.yaml`

```yaml
# =============================================================================
# Stage Runner: {Name}
# =============================================================================

stages:
  {name}:
    # Absolute path to the stage's Python interpreter (its own venv)
    python:   /path/to/{name}/venv/Scripts/python.exe
    # Absolute path to the stage entry point
    script:   /path/to/{name}/main.py
    # Default args passed on every invocation — env is appended at runtime
    args:
      - --env
```

**Rules:**

- Each stage is a fully independent Python project with its own venv
- `python` always points into the stage's own venv — never the orchestrator's interpreter
- `args` lists fixed CLI arguments; the orchestrator appends the environment arg at runtime
- `env` is always appended by the orchestrator — do not hard-code it here

---

## Secrets and Credentials

Credentials are **never** stored in YAML files. The pattern is:

1. `.env` file at the project root holds raw credentials:
   ```
   SQLSERVER_NAVICONTROL_USER=sa
   SQLSERVER_NAVICONTROL_PASSWORD=my_password
   ```

2. `config_utils.py` calls `inject_secrets()` at startup, which reads `.env` via `python-dotenv` and injects values into the relevant `ctx` attributes.

3. The YAML file documents the expected env var names in a comment:
   ```yaml
   # user and password injected from .env at startup via inject_secrets()
   # SQLSERVER_NAVICONTROL_USER / SQLSERVER_NAVICONTROL_PASSWORD
   ```

**Rules:**

- `.env` is always in `.gitignore` — never commit it
- Env var names follow the pattern: `{PROVIDER}_{DBNAME}_{FIELD}` — all uppercase, underscores only
- If a field has no corresponding env var (e.g. Windows Auth with no user), omit the field from YAML entirely — do not set it to blank or null

---

## Path Rules

These rules apply to every path value in every config file:

| Path starts with | Interpretation |
|---|---|
| `~/` | Relative to the current user's home directory — use for dev |
| Anything else | Treated as an absolute path — use for prod |

`config_utils.py` resolves `~` via `pathlib.Path.expanduser()` before populating `ctx`. All code reads the resolved absolute path from `ctx` — never raw path strings from config.

---

## Adding a New Named Instance

To add a new database, data source, broker, or other named instance:

1. Create a new sub-config file following the naming convention: `config.{section}.{name}.yaml`
2. Populate it using the appropriate template from this guide
3. Add any required credentials to `.env`
4. Restart the application

No other files change. `config_utils.py` discovers the new file automatically on next startup.

---

## Adding a New Section

If no existing section covers your use case, define a new one:

1. Choose a short, lowercase section name (e.g. `broker`, `exchange`, `report`)
2. Create the first sub-config file: `config.{section}.{name}.yaml`
3. Update `AppContext` in `ctx.py` to include the new section's attributes with type hints
4. Update `config_utils.py` to parse and populate the new section into `ctx`
5. Add the section to this guide

Do not reuse an existing section name for a different concept.

---

## Complete File Name Reference

| File | Purpose |
|---|---|
| `config.dev.yaml` | Main config — dev environment |
| `config.prod.yaml` | Main config — prod environment |
| `config.db.{Provider}_{DbName}.yaml` | One database connection |
| `config.data_source.{name}.yaml` | One external data feed |
| `config.stages.{name}.yaml` | One external stage runner |
| `config.broker.{name}.yaml` | One broker definition (example of custom section) |

---

## Checklist — Before Committing a New Config File

- [ ] File name follows `config.{section}.{name}.yaml` exactly
- [ ] Top-level YAML key matches the section name
- [ ] No credentials, tokens, or passwords in the file
- [ ] Credentials documented as comments with the expected env var names
- [ ] All paths follow the path rules (absolute for prod, `~/` for dev)
- [ ] `AppContext` in `ctx.py` has been updated if new attributes are introduced
- [ ] `config_utils.py` parses the new section (if new section added)
- [ ] `.env` updated with any new credentials
- [ ] `.gitignore` excludes `.env`

---

*End of Guide*
