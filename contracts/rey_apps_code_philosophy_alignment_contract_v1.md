# Rey Apps Code Philosophy Alignment Contract v1

## Purpose

Align all Rey Apps with the programming philosophy before the codebase grows more surface area.

This contract covers all apps under `development/apps` except `trade_analyzer`.

Apps covered:

- `rey_lib`
- `rey_console`
- `rey_loader`
- `file_redactor`
- `rey_analyzer`
- `pipeline_coordinator`
- `ftp_sync`

The goal is not cosmetic cleanup.

The goal is to remove duplicate infrastructure, enforce shared source-of-truth behavior, prevent silent failures, and make future app expansion predictable.

---

## Core Philosophy

Rey Apps follow this dependency direction:

```text
main.py -> app package -> rey_lib
```

Rules:

- Reusable infrastructure belongs in `rey_lib`
- App packages contain app-specific orchestration and business rules only
- Apps must not reimplement config assembly, file movement, file discovery, logging, error handling, or shared preview behavior
- Config files and logs are source-of-truth artifacts
- Development changes are made and committed from `development`, then pulled into `Test` for testing
- Inbox files are never read by automation or LLM assistance

---

## Non-Negotiable Boundaries

### 1. Inbox Safety

No task may read files under any `inbox` folder.

Allowed:

- Read app code
- Read config structure
- Read reference YAML
- Read logs
- Read done/processed output files when explicitly needed

Forbidden:

- `cat`, `sed`, `head`, parser reads, previews, or sampling from `*/inbox/*`
- Broad scans that emit inbox file contents
- Using inbox file data to infer schemas

If a schema needs to be inferred, use redacted output, profile output, logs, references, or an approved synthetic fixture.

### 2. Development Source of Truth

All code edits and commits must originate in:

```text
development/apps
development/installations
```

`Test` is for pulling and verifying committed development changes.

Do not commit from `Test`.

### 3. No Duplicate Shared Logic

Before adding app code, check `rey_lib`.

If the behavior is reusable by more than one app, it belongs in `rey_lib`.

Duplicate logic to remove first:

- Config assembly
- File discovery
- File movement and movement state logging
- File preview and safe path validation
- File pattern matching
- Error normalization and logging
- LLM timeout/rate-limit handling

---

## Priority Order

## Priority 0 — Stabilize Existing In-Flight Changes

### Goal

Finish or intentionally revert the current uncommitted alignment work before beginning new feature work.

### Required Work

- Verify `rey_console` still renders after the latest static file cleanup
- Verify `rey_console` browser errors route through server error utilities
- Verify `rey_console` config views call `rey_lib.config.config_utils`
- Verify `rey_lib.files.file_utils` exports the shared safe file utilities
- Run tests for `rey_console` and `rey_lib`
- Commit only from `development`

### Acceptance Criteria

- `rey_console` tests pass
- `rey_lib` tests pass
- No active console behavior is removed without explicit approval
- No unused frontend scaffold remains that confuses the active architecture

---

## Priority 1 — Fix `rey_loader` / `rey_lib.file_loader` Contract Break

### Problem

`rey_loader` tests currently fail because `rey_lib.files.file_loader` expects `columns` to be an iterable list of column configs, while current reference/config shapes allow column mappings.

Observed failure class:

```text
TypeError: attribute name must be string, not 'int'
```

### Required Work

- Fix the shared loader implementation in `rey_lib.files.file_loader`
- Support current reference YAML structure
- Preserve backward compatibility with existing valid loader configs
- Ensure `columns` mapping and list forms are normalized once
- Ensure `field_transforms` and secrets are resolved from the same normalized shape
- Do not patch around this in `rey_loader`

### Acceptance Criteria

- `rey_lib` tests pass
- `rey_loader` tests pass
- Loader reference YAML and loader contract agree
- No app-specific loader parsing exists in `rey_loader`

---

## Priority 2 — Centralize File Handling in `rey_lib`

### Problem

Multiple apps still own some file handling behavior directly.

Apps should not implement their own file handlers when the behavior is generic.

### Required Work

Move or consolidate shared behavior into `rey_lib.files`:

- File discovery
- Recursive pattern matching
- Extension filtering
- Hidden file filtering
- Safe path resolution
- Safe text previews
- Folder tree listing
- File movement
- File movement state logging
- Original-path lookup from movement history

Apps may keep only app-specific orchestration around these utilities.

### Acceptance Criteria

- `rey_console` uses shared preview/discovery/path utilities
- `rey_analyzer` uses shared file discovery and filters
- `file_redactor` uses shared movement and file selection utilities
- `rey_loader` uses shared file loading utilities
- No app package contains generic file finder or mover logic

---

## Priority 3 — Movement State Log and Reset Correctness

### Problem

Reset pipeline files must return files to their original starting folders, not just a guessed inbox folder.

Logs showed that older pipeline movement history may be required to determine the true origin.

### Required Work

- Maintain a shared movement state log in `rey_lib.files`
- `move_file` writes to the state log only after a successful move
- Provide a utility to find the current path and original path of a moved file
- Reset logic reads movement history from oldest to newest
- Reset preview must show exact planned moves before execution
- Reset execution must move files only, never silently delete

### Acceptance Criteria

- A file moved through multiple pipeline folders resets to its absolute original folder
- Reset preview and reset execution agree
- Missing movement history is reported clearly
- Reset is disabled or guarded in production configs

---

## Priority 4 — Unified Config Views

### Problem

The console displayed multiple YAML files separately, which makes the runtime configuration hard to understand.

The console must show the assembled config that the app actually uses.

### Required Work

- Split config utilities so the same code path can:
  - read YAML sources
  - assemble the final config
  - return source context for human display
- The console must call `rey_lib.config.config_utils` for assembly
- The console must not duplicate merge behavior
- Current config view must show the full assembled app or pipeline config
- Executed config view must show the exact assembled config captured at run time
- Assembled YAML must include source comments identifying the file that contributed each block

### Acceptance Criteria

- Console current config view and runtime `ctx` assembly use the same underlying code
- Pipeline objects expose current and executed config options
- Logs include exact executed config state
- No app-local YAML merge implementation exists in `rey_console`

---

## Priority 5 — LLM Timeout and Rate-Limit Handling

### Problem

Files may fail silently when LLM requests time out or receive rate-limit responses.

Too many request/time-out responses must not be treated as successful processing.

### Required Work

- Centralize LLM request warning/failure interpretation
- Log timeouts as warnings
- Log rate-limit responses as warnings
- Track timeout/rate-limit counts per run
- Promote excessive timeout/rate-limit counts to failure
- Ensure failed LLM processing moves files to failed/rejected, not processed/done

### Acceptance Criteria

- A file with failed LLM output cannot be moved to processed/done as if successful
- Logs clearly show timeout and rate-limit warnings
- Failure threshold is configurable
- Pipeline exit status reflects excessive LLM failures

---

## Priority 6 — Redactor Numeric Fidelity

### Problem

Numeric redaction can distort data enough that downstream type inference chooses incorrect types.

All numeric fields should not become generic `DECIMAL(20, 8)`.

### Required Work

- Redactor detects integer vs decimal values
- Redactor tracks maximum integer digits per column
- Redactor tracks maximum decimal scale per column
- Redactor preserves numeric scale during redaction
- Decimal redaction should produce structurally similar values
- Integer redaction should remain integer
- Add integer distribution metadata where useful for downstream type decisions
- Use one shared redaction path for `redact_all` and explicit column redaction

### Acceptance Criteria

- Redacted integer columns remain integer-like
- Redacted decimal columns preserve observed scale
- Profile metadata distinguishes integer and decimal candidates
- Loader/analyzer has enough metadata to choose better numeric SQL types
- `redact_all` and explicit columns produce consistent behavior through one code path

---

## Priority 7 — File Pattern and Multi-Setup Support

### Problem

Some file flows need multiple file extensions or multiple setup definitions in one YAML.

Analyzer behavior also appeared to ignore extension filters in some cases.

### Required Work

- Centralize extension and pattern filtering in `rey_lib.files`
- Ensure `rey_analyzer` and `rey_loader` use the same filter utility
- Allow multiple file patterns where the config contract permits it
- Decide whether multiple setups per YAML are required
- Update reference YAML and contracts to match the chosen shape

### Acceptance Criteria

- Extension filters are honored consistently by all apps
- Multi-pattern configs are represented in reference YAML
- Analyzer and loader use the same file selection code
- No app-specific extension filtering remains

---

## Priority 8 — Shared Installation Config Location

### Problem

Some configs, such as the app registry, are installation-level shared configs and should not live inside one app's config folder.

### Required Work

- Define shared installation config location
- Move shared app registry for development and Test installations
- Update config utils lookup rules to support shared installation configs
- Ensure apps can load shared configs without reading every app's config folder
- Keep app-specific configs inside app config folders

### Acceptance Criteria

- Shared registry lives in an installation-level shared config location
- Apps read shared registry through config utilities
- No app directly scans every app config folder to find shared registry data
- Reference folder includes a copy for LLM assistance

---

## Priority 9 — Console Architecture Alignment

### Problem

`rey_console` is functionally useful but still has large modules and a large JavaScript file.

The app must not become confusing bloat as it expands.

### Required Work

Backend:

- Keep route handlers thin
- Move app-specific logic into focused modules
- Move reusable utilities to `rey_lib`
- Keep config views dependent on config utilities
- Route browser/client errors through server error utilities

Frontend:

- Keep HTML in template files
- Keep JS in JS files
- Avoid unused frontend scaffolding
- Keep active frontend architecture explicit
- Preserve three-pane operational layout
- Preserve resizable panes
- Preserve viewer fullscreen/popout behavior
- Keep run and reset buttons

### Acceptance Criteria

- `routes.py` is dispatch only
- `inventory.py` and `operations.py` are decomposed into focused modules where practical
- `console.js` is either intentionally accepted as the active baseline or replaced by a committed component architecture
- No duplicate browser error handling patterns exist
- No dead frontend architecture is kept beside the active one

---

## Priority 10 — FTP Sync Tuple Compatibility

### Problem

`ftp_sync` tests fail because tests pass two-item file tuples while shared FTP sync logic expects three-item tuples including size.

### Required Work

- Decide the authoritative remote file metadata shape
- Update tests or compatibility normalization accordingly
- Keep FTP filtering logic in `rey_lib.ftp`
- Do not patch individual FTP app code to compensate

### Acceptance Criteria

- `ftp_sync` tests pass
- FTP metadata shape is documented in reference YAML or app docs
- Filter behavior is consistent for name, extension, age, and size

---

## Priority 11 — Contracts and Reference YAML Alignment

### Problem

LLM-facing markdown contracts and reference YAML must agree so YAML can be authored without reading app code.

### Required Work

- Align `rey_loader` markdown instructions with `rey_loader.reference.yaml`
- Ensure every app reference file includes:
  - config file names
  - CLI parameters
  - allowed values
  - examples
  - environment behavior
  - shared config dependencies
- Use `app_name.reference.yaml` naming
- Keep reference files under installation reference folders
- Include app registry reference copy

### Acceptance Criteria

- An LLM can write valid YAML using reference files alone
- Reference files do not require reading source code to know allowed values
- Reference examples avoid live configs
- Live configs are not touched while updating references

---

## Priority 12 — App-Specific Cleanup

### `file_redactor`

Required:

- Keep one redaction code path
- Preserve numeric scale
- Keep file movement through `rey_lib.files`
- Avoid direct inbox content reads during development assistance

Acceptance:

- Tests pass
- Redactor reference matches actual behavior

### `rey_analyzer`

Required:

- Use shared file filtering
- Honor extensions and patterns
- Avoid generic file handler implementations

Acceptance:

- Tests pass
- Analyzer does not pick files outside configured filters

### `pipeline_coordinator`

Required:

- Replace `print` with logging where appropriate
- Keep subprocess execution explicit and logged
- Avoid broad catches outside approved error boundaries
- Ensure assembled config is logged for runs

Acceptance:

- Tests pass
- Run logs clearly show command, config state, warnings, failures, and exit status

### `rey_console`

Required:

- Keep console as operational viewer/runner, not config owner
- Display current and executed config through shared config utilities
- Preserve viewer fullscreen/popout
- Keep reset preview explicit

Acceptance:

- Tests pass
- UI renders
- Config displays are unified and human-readable

### `rey_loader`

Required:

- Do not duplicate loader parsing
- Rely on `rey_lib.files.file_loader`
- Match loader reference YAML

Acceptance:

- Tests pass
- Column mapping/list compatibility is resolved in `rey_lib`

### `ftp_sync`

Required:

- Normalize or document FTP metadata tuple shape
- Keep filtering in `rey_lib.ftp`

Acceptance:

- Tests pass

---

## Work Sequence

Use this sequence unless a production issue requires interruption:

1. Stabilize in-flight `rey_console` and `rey_lib` changes
2. Fix `rey_loader` by fixing `rey_lib.files.file_loader`
3. Centralize file handling and movement state in `rey_lib`
4. Fix reset pipeline behavior using movement history
5. Complete unified config views and executed config logging
6. Fix LLM timeout/rate-limit warning and failure behavior
7. Fix redactor numeric fidelity and single redaction path
8. Fix shared file pattern filtering and multi-pattern support
9. Move shared installation configs and registry lookup
10. Continue console decomposition
11. Fix `ftp_sync` tuple/test compatibility
12. Align all reference YAML and markdown contracts

---

## Verification Matrix

Before each priority is considered complete:

- Run affected app tests
- Run affected `rey_lib` tests
- Confirm no inbox file content was read
- Confirm no Test commits were made
- Confirm reusable logic moved to or remained in `rey_lib`
- Confirm app code did not duplicate shared utilities
- Confirm logs report failures and warnings explicitly
- Confirm reference YAML matches the implemented behavior when config shape changes

---

## Commit Rules

- Commit from `development` only
- Commit changed apps separately when practical
- Version bump only changed apps unless explicitly requested otherwise
- Do not commit generated test data unless explicitly requested
- Do not commit live config changes unless the task explicitly requires live config changes

---

## Completion Definition

This alignment work is complete when:

- All covered app test suites pass
- `rey_loader` and `ftp_sync` failures are resolved
- File handling is shared through `rey_lib`
- Config assembly is shared through `rey_lib.config.config_utils`
- Redactor numeric output supports better downstream type inference
- LLM warning/failure behavior prevents silent successful moves
- Console remains a thin operational UI over filesystem, configs, logs, CLI, and `rey_lib`
- Reference YAML and markdown contracts match the running system

---

**End of Contract**
