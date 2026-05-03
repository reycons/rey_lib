# MySQL Stored Procedure Rules (Authoritative)

These rules define the canonical structure and comment/section formatting for **all** MySQL stored procedures.

This document is authoritative and overrides assistant defaults.  [oai_citation:0‡MySQL Procedures Formatting.md](sediment://file_00000000f0b471fdb0b931a276c8d814)

---

## 1. Mandatory Procedure Signature

- Every procedure MUST include `inParentBatchStepID` as the first parameter.
- `inParentBatchStepID = 0` indicates a top-level procedure.
- `inParentBatchStepID <> 0` indicates a nested procedure.
- Nested procedures MUST inherit the parent batch.
- Nested procedures MUST NEVER close the batch.

---

## 2. Standardized Internal Variables

All procedures MUST declare and use the following variables exactly (names are mandatory):

- `vProcName` — reproducible procedure call string
- `vMessage` — log message representing the procedure invocation
- `vSQLCmd` — SQL text passed to execution wrappers
- `vBatchID` — resolved batch identifier
- `vProcBatchStepID` — root batch step for the procedure invocation
- `vLastBatchStepID` — most recent batch step identifier
- `vRowCount` — row count captured from executed SQL

Additional variables may be declared, but the above variables MUST always exist and be used.

---

## 3. Reproducible Procedure Call String

- `vProcName` MUST begin as a template containing placeholders for **all** input parameters.
- All placeholders MUST be replaced before logging.
- String parameters MUST be quoted (use `QUOTE()`).
- The final log message MUST represent the complete procedure call (replayable from logs).

---

## 4. Batch Resolution

Every procedure MUST resolve a batch before performing any work.

- Top-level procedures MUST create a new batch.
- Nested procedures MUST resolve the batch from the parent batch step.
- Procedures MUST NOT proceed without a valid batch context.

---

## 5. Root Batch Step

- Every procedure MUST create a root batch step representing the procedure invocation.
- This root step is the parent of all subsequent steps.
- All logging within the procedure MUST descend from this root step.
- The root step MUST persist for the lifetime of the procedure execution.

---

## 6. Step-Level Logging

Every procedure MUST log at minimum:

- `Step 1: Start`
- `Step 2: Execute logic`
- `Step 3: End`

Rules:
- Each logical operation MUST be bracketed by a batch step.
- The most recent batch step MUST always be tracked.
- Required steps MUST NOT be removed or renamed.
- Additional steps MAY be added as needed.

---

## 7. SQL Execution

All substantive SQL MUST be executed through logging-aware wrappers.

Rules:
- SQL MUST be assigned to `vSQLCmd` before execution.
- Single-statement and multi-statement executions MUST be logged.
- Row counts MUST be captured when relevant.
- Direct execution of data-changing SQL inside the procedure body is NOT allowed unless explicitly requested.

---

## 8. Batch Closure

- Only top-level procedures MAY close a batch.
- Top-level procedures MUST close the batch exactly once.
- Nested procedures MUST NEVER close the batch.

---

## 9. Canonical Comment and Section Header Formatting

All procedures MUST use section headers exactly in this format (matching the template procedure):

- Each section header is a **3-line block**:
	- Line 1: `-- ------------------------------------------------------------`
	- Line 2: `-- <N>. <Section Title>`
	- Line 3: `-- ------------------------------------------------------------`
- There MUST be exactly **one space** after `--` on each header line.
- The dashed line MUST be exactly:
	- `-- ------------------------------------------------------------`
- Section titles MUST be numbered with the pattern:
	- `0. ...` for standardized declarations
	- `1. ...` onward for procedural flow
- Each major section MUST be separated by a blank line after the header block.
- Sub-examples inside a section MUST use plain `--` comments (no dashed separators).

Required canonical section titles (minimum set):
- `0. Standard logging variables (standardized names)`
- `1. Build reproducible vProcName string for logging`
- `2. Create or look up batch`
- `3. Start batch step logging (proc root step)`
- `4. Main logic section`
- `5. End logging and close batch if top-level`

Notes:
- Additional numbered sections MAY be inserted between 4 and 5 (e.g., `4.1`, `4.2` is allowed only if you explicitly want it; otherwise continue integer numbering).
- Do NOT use block comments (`/* ... */`) for sectioning.

---

## 10. Canonical Procedure Skeleton

All procedures MUST follow this high-level order:

1) Declare standard logging variables  
2) Build `vProcName` and `vMessage`  
3) Resolve `vBatchID`  
4) Create procedure root batch step and required Step 1/2/3 steps  
5) Execute logic only via wrappers  
6) Close batch only when top-level  

---