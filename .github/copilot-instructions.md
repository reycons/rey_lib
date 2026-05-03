# Programming Assistant Contract (Authoritative)

**Version:** 1.1
**Last Updated:** 2026-05-02

This contract governs behavior, correctness, and engineering discipline for all SQL and Python assistance.

Full SQL formatting rules are defined in `contracts/SQL Formatting.md`.
MySQL procedure rules are in `contracts/MySQL Procedures Formatting.md`.
SQL Server procedure rules are in `contracts/SQLServer_Procedures_Formatting.md`.

---

## 1. Accuracy First

- Do not guess, hallucinate, or invent syntax, functions, flags, or features
- If uncertain, say so explicitly or ask **one concise clarifying question**
- Prefer conservative, well-known approaches over clever or novel ones
- Do not reframe or reinterpret requirements unless explicitly asked

---

## 2. Output Discipline

- Output **only what is requested**
- Do not add explanations unless explicitly requested
- Assume all code will be **copy-pasted into production**
- Avoid verbosity, commentary, or filler

---

## 3. SQL Behavioral Rules

- Follow the SQL Contract exactly — formatting, structure, and procedures
- Optimize for large tables (hundreds of millions to multi-TB), predicate-driven queries, reporting-heavy workloads, zero-downtime environments
- Avoid temp tables unless explicitly requested
- Chunk large operations
- Procedures must be restart-safe

---

## 4. Stored Procedure Standards

- Follow the MySQL/SQL Server Stored Procedure rules in the SQL Contract exactly
- Use consistent logging and batch step patterns
- Log all dynamic SQL
- Avoid hidden side effects
- Prefer deterministic behavior

---

## 5. Python Engineering Rules

- Prefer clarity over cleverness
- No unnecessary abstractions
- Explicit error handling — never silent failures
- Log instead of print
- No hidden globals
- Functions must do one thing
- Code must be readable by another engineer in 6 months
- All code must be commented
- Type hints on all function signatures, including explicit return types (`-> str`, `-> None`, `-> list[str]`)
- No commented-out code in production files
- Use `pathlib` for all file and path operations — never string-concatenate paths
- All functions, classes, and modules must have docstrings — comments and docstrings are not the same thing; both are required
- Import ordering: stdlib → third-party → local; one blank line between each group
- Wildcard imports are forbidden (`from x import *`)
- Code must comply with PEP 8
- Line length must not exceed 100 characters
- All projects must use a virtual environment (`venv`) — never install dependencies globally
- Functions must never be overly large or complex
- Large operations must be decomposed into focused helper functions, each doing exactly one thing
- A function that is doing multiple logical steps must be refactored — each step becomes its own helper and the parent function orchestrates the calls
- If a function requires significant scrolling to read, it is too large

**Naming conventions:**

- Functions and variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private functions and variables: prefix with single underscore `_name`
- Module-level public API must be declared with `__all__`

**Common Python pitfalls — always avoid:**

- Mutable default arguments are forbidden (`def f(x=[])` and `def f(x={})`) — use `None` and assign inside the function
- The `global` keyword is forbidden — pass values explicitly or use `ctx`
- Broad `except Exception` is forbidden outside `error_utils.py`
- Circular imports are forbidden — if two modules need each other, the shared logic must move to a third module
- Always use context managers (`with` statements) for any resource that requires cleanup — database connections, file handles, locks; never open and close manually

**Exception chaining:**

- Always use `raise NewException("message") from original` when re-raising — never lose the original traceback
- Bare `raise NewException()` without `from original` is forbidden when re-raising inside an `except` block

**Cannot-comply rule:**

- If any rule in this contract cannot be followed for a specific request, explicitly state which rule and why before proceeding — do not silently deviate

---

**Module and Function Placement:**

- Every function must live in the most appropriate module for its responsibility — never place a function in a file because it is convenient
- If a function does not clearly belong to an existing module, create a new focused module rather than misplacing it
- All reusable, generic logic belongs in `rey_lib` — never in a consuming project
- Never duplicate logic — always import and reuse it

---

### 5.1 Configuration

- Never hard-code any value — always use config files
- All config files must be **YAML format**
- Main config file naming convention: `config/config.dev.yaml`, `config/config.prod.yaml`
- For local development secrets, use a `.env` file loaded via `python-dotenv` — never commit `.env` to source control
- Secrets in YAML config are declared via an `env:` block — rey_lib resolves them from the environment automatically
- All config is loaded into a `ctx` (context) object at startup via `build_ctx()` — never call this more than once
- `ctx` is the single source of truth for all runtime configuration and state
- The environment is always passed as a CLI argument — never inferred or hard-coded

---

### 5.2 Security

- Secrets and credentials must never appear in config files or source code in plain text
- Use environment variables or a `.env` file for credentials
- Never log credentials, tokens, or PII under any circumstances
- All SQL must use **parameterized queries** — string-formatted SQL is forbidden
- All projects must include a `.gitignore` that excludes at minimum: `.venv/`, `.env`, `*.pyc`, `__pycache__/`, any secrets or key files
- Sensitive data must not be written to log files
- Do not expose stack traces or internal error details in any output intended for end users

---

### 5.3 Project Structure

```
main.py                    # Orchestration only — no business logic
config/
    config.dev.yaml
    config.prod.yaml
    db/
    data_feeds/
    app/
.env                       # Local secrets — never committed
.gitignore
requirements.txt           # All dependencies pinned
README.md
.venv/
{project_name}/            # App-specific modules only
    __init__.py
    db.py
    # One module per functional area
sql/
    {server}/
tests/
    conftest.py
    {project_name}/
```

---

### 5.4 SQL Query Files

- All SQL queries must be defined in `.sql` files under `sql/{server}/`
- SQL file naming convention: `{verb}_{subject}.sql`
- Dynamic values must use **named replace strings** as placeholders — e.g. `{schema_name}`, `{batch_id}`
- SQL files must follow the SQL Contract formatting rules exactly
