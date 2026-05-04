# YAML Configuration File Guide

**Version:** 2.0  
**Applies to:** All Python projects governed by the LLM Programming Contract

---

## Overview

Projects use a layered YAML configuration model under a `config/` directory:

- **Main config files** — one per environment: `config/config.dev.yaml`, `config/config.prod.yaml`
- **Sub-config files** — additional YAML files under named subfolders (for example `config/db/`, `config/data_feeds/`, `config/app/`)

At startup, `build_ctx(env=..., project_root=...)` loads and merges all YAML files into one runtime `ctx` object. Application code reads from `ctx` only.

---

## Required Directory Layout

```text
config/
  config.dev.yaml
  config.prod.yaml
  db/
  data_feeds/
  app/
```

Rules:

- Main config files must be named exactly `config.dev.yaml` and `config.prod.yaml` inside `config/`
- Additional config files belong in a named subfolder under `config/`
- Keep one functional area per subfolder (`db`, `data_feeds`, `llm`, `stages`, etc.)

---

## Main Config File (`config/config.{env}.yaml`)

Main config files contain singleton settings and the environment key registry.

### Template

```yaml
# Main config ({env})

log_path: ~/python/logs/{project}.{operation}.{timestamp}.log
log_level: INFO

# Central environment key registry.
env:
  - name: service_api_key
    env_var: SERVICE_API_KEY
    generate: false
```

Rules:

- Keep only singleton values here (for example log defaults, app mode flags)
- Use top-level `env` list to map logical key names to environment variables
- Do not place passwords/tokens directly in YAML

---

## Environment Key Registry and `env.<name>` References

Use the top-level `env` block in main config to declare all environment-backed values.

### Registry declaration

```yaml
env:
  - name: ftp_user_client01
    env_var: FTP_USER_CLIENT01
    generate: false
  - name: ftp_password_client01
    env_var: FTP_PASSWORD_CLIENT01
    generate: false
```

### Usage in downstream YAML

```yaml
connections:
  - name: client01
    ftp:
      user: env.ftp_user_client01
      password: env.ftp_password_client01
```

Behavior:

- `build_ctx()` resolves `env.<name>` by looking up matching `name` in the main config `env` list
- If `env_var` is missing in `.env`/environment, resolved value is empty string and a warning is logged
- Unknown `env.<name>` references raise `ConfigError`

---

## `generate: true` for Managed Keys

Use `generate: true` only for values the project is expected to generate automatically (for example Fernet keys).

Example:

```yaml
env:
  - name: account_encryption_key
    env_var: ACCOUNT_ENCRYPTION_KEY
    generate: true
```

Key generation helpers in `rey_lib.encryption` create missing values in `.env` without overwriting existing ones.

---

## Sub-Config File Patterns

Use sub-config files for named collections and per-instance settings.

### Database and load definitions (`config/db/*.yaml`)

```yaml
connections:
  - name: duckdb
    provider: duckdb
    path: ~/python/data/project/data.duckdb

loads:
  - name: transaction
    destination:
      type: database
      connection: duckdb
      table: transaction
```

### Data feed / connection definitions (`config/data_feeds/*.yaml`)

```yaml
connections:
  - name: client01
    ftp:
      host: ftp.client01.com
      user: env.ftp_user_client01
      password: env.ftp_password_client01
    sync:
      chunk_size: 50
      remote_paths: [/incoming/]
```

Rules:

- Put per-instance operational settings with the instance (for example `sync.chunk_size` per connection)
- Do not keep per-connection settings as global singleton values unless all connections must share one value

---

## Secrets Rules

- Never store plaintext credentials in YAML
- Keep `.env` out of source control (`.gitignore` must exclude it)
- Store only logical references in YAML (`env.<name>`)
- Do not use legacy `inject_secrets()` patterns in app code for new work; use `env` registry + `env.<name>` resolution

---

## Path Rules

These apply to all YAML path values:

| Path form | Meaning |
|---|---|
| `~/...` | Relative to current user home directory |
| absolute path | Used as-is |

`build_ctx()` resolves paths before values are consumed by application code.

---

## Adding New Config Safely

1. Decide whether value is singleton or per-instance.
2. For singleton values: place in `config/config.{env}.yaml`.
3. For per-instance values: place in the corresponding file under `config/<area>/`.
4. If value is secret-backed:
   1. Add an entry to top-level `env` list in main config.
   2. Reference it downstream as `env.<name>`.
5. Restart application and verify `ctx` resolves expected values.

---

## Checklist Before Commit

- [ ] Main config files are in `config/config.dev.yaml` and `config/config.prod.yaml`
- [ ] New files are in a named `config/` subfolder
- [ ] No secrets in YAML
- [ ] Every `env.<name>` has a matching top-level `env` declaration
- [ ] Per-instance operational settings are stored per instance (not globally)
- [ ] `.env` remains gitignored

---

*End of Guide*
