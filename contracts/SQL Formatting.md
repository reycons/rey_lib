# SQL Contract (Authoritative)





This document defines canonical SQL formatting and MySQL stored procedure standards.


It overrides all assistant defaults. If a rule conflicts with your defaults, **these rules win**.


If you cannot comply with a rule, **explicitly state why** instead of guessing.





-----





## LLM PROMPT — READ FIRST





Before outputting **any** SQL, verify:





- Tab indentation (never spaces)


- Leading commas on all multi-line lists


- All SQL keywords UPPERCASE


- No blank lines between clauses


- Semicolon on its own final line


- Original table and column names preserved exactly


- No subqueries — use CTEs


- No `WHERE 1=1`





-----





## Part 1 — SQL Formatting





-----





### Rule 1 — Indentation





- Use **tabs only**, never spaces.


- Indent content inside a clause (`SELECT`, `CASE`, `JOIN`, etc.) by **one tab** under its parent keyword.





-----





### Rule 2 — SQL Keywords





- All SQL keywords must be **UPPERCASE**.


- Examples: `SELECT`, `FROM`, `CASE`, `WHEN`, `THEN`, `ELSE`, `END`, `JOIN`, `ON`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `QUALIFY`, `WITH`, `AS`, `AND`, `OR`, `NOT`, `IN`, `EXISTS`.





-----





### Rule 3 — Comma Placement





- Use **leading commas**:


  - In `SELECT` lists


  - In multi-line lists


  - In CTE definitions


- When items appear on the **same line**, use standard commas with **exactly one space** after each comma.


- **Aliased columns** must appear **one per line**.


- **Non-aliased columns** may be grouped on a single line.





-----





### Rule 4 — CASE Expressions





- Use the **searched CASE** form only.


- Vertically align `CASE`, `WHEN`, `THEN`, `ELSE`, and `END`.


- Add an inline comment on the `CASE` line indicating the final column name.


- If wrapped in parentheses, place the closing parenthesis on its **own line**.





```sql


CASE -- column_name


	WHEN condition THEN value


	WHEN condition THEN value


	ELSE value


	END


```





-----





### Rule 5 — Comments





- Use `--` for inline comments only. Never use block comments (`/* ... */`).


- Structured comment blocks are **optional**, used only for full scripts, production queries, or when explicitly requested.


- Allowed structured sections: **PURPOSE**, **LOGIC**, **LINEAGE** (INPUT / OUTPUT), **OUTPUT COLUMNS**.


- Structured blocks must be surrounded by dashed lines.





```text


---


PURPOSE:


LOGIC:


LINEAGE:


	INPUT:


	OUTPUT:


OUTPUT COLUMNS:


---


```





-----





### Rule 6 — JOINs and ON Conditions





- Base table appears on the **same line as `FROM`**.


- Each `JOIN` is indented **one tab**.


- `ON` is indented **one additional tab**; first predicate on the **same line**.


- Additional predicates use **leading `AND`**, one per line, same indentation as the first predicate.





```sql


FROM base_table


	INNER JOIN joined_table


		ON base_table.id = joined_table.id


		AND joined_table.active_flg = 1


```





-----





### Rule 7 — Line Breaks and Wrapping





- Do **not** force every column onto its own line.


- Aliased columns are **always one per line**.


- Group simple non-aliased columns where it improves readability.


- **No blank lines between clauses.**





-----





### Rule 8 — CTEs (`WITH`)





- Use **leading commas** before each CTE after the first.


- Indent each CTE body **one tab** under `WITH`.


- Prefer CTEs over subqueries in all cases — **do not use subqueries**.


- Long `IN` lists MUST be moved to a CTE and joined, never inlined.


- If a subquery appears in existing code being modified, do **not** refactor it unless explicitly instructed.





```sql


WITH base AS (


	SELECT


		col1


		, col2


	FROM table_a


)


, derived AS (


	SELECT


		col1


	FROM base


)


```





-----





### Rule 9 — Naming Preservation





- **Do not rename** tables or columns unless explicitly instructed.


- Preserve spelling, case, and structure exactly as provided.


- Do not clean up or normalize names.





-----





### Rule 10 — SELECT Statement Structure





- `SELECT` starts at the **left margin**.


- Columns indented **one tab**.


- Use **leading commas** for all columns after the first.





```sql


SELECT


	key_field


	, field_1 AS alias


	, field_2, field_3


FROM table_name


```





-----





### Rule 11 — Semicolons





- The semicolon terminating a statement appears on its **own line**, after the final clause.





```sql


ORDER BY


	key_field


;


```





-----





### Canonical Reference Example





```sql


WITH filtered AS (


	SELECT


		key_field


		, metric_value AS metric_value


		, created_dt, updated_dt


	FROM fact_table


		INNER JOIN dim_table


			ON fact_table.dim_id = dim_table.dim_id


	WHERE fact_table.active_flg = 1


		AND dim_table.region_cd = 'US'


)


SELECT


	key_field


	, metric_value


	, created_dt, updated_dt


FROM filtered


GROUP BY


	key_field, metric_value, created_dt, updated_dt


ORDER BY


	key_field


;


```





-----





## Part 2 — MySQL Stored Procedures





-----





### Rule P1 — Mandatory Procedure Signature





- Every procedure MUST include `inParentBatchStepID` as the first parameter.


- `inParentBatchStepID = 0` indicates a top-level procedure.


- `inParentBatchStepID <> 0` indicates a nested procedure.


- Nested procedures MUST inherit the parent batch.


- Nested procedures MUST NEVER close the batch.





-----





### Rule P2 — Standardized Internal Variables





All procedures MUST declare and use the following variables exactly (names are mandatory):





- `vProcName` — reproducible procedure call string


- `vMessage` — log message representing the procedure invocation


- `vSQLCmd` — SQL text passed to execution wrappers


- `vBatchID` — resolved batch identifier


- `vProcBatchStepID` — root batch step for the procedure invocation


- `vLastBatchStepID` — most recent batch step identifier


- `vRowCount` — row count captured from executed SQL





Additional variables may be declared, but the above MUST always exist and be used.





-----





### Rule P3 — Reproducible Procedure Call String





- `vProcName` MUST begin as a template containing placeholders for **all** input parameters.


- All placeholders MUST be replaced before logging.


- String parameters MUST be quoted (use `QUOTE()`).


- The final log message MUST represent the complete procedure call (replayable from logs).





-----





### Rule P4 — Batch Resolution





- Top-level procedures MUST create a new batch.


- Nested procedures MUST resolve the batch from the parent batch step.


- Procedures MUST NOT proceed without a valid batch context.





-----





### Rule P5 — Root Batch Step





- Every procedure MUST create a root batch step representing the procedure invocation.


- This root step is the parent of all subsequent steps.


- All logging within the procedure MUST descend from this root step.


- The root step MUST persist for the lifetime of the procedure execution.





-----





### Rule P6 — Step-Level Logging





Every procedure MUST log at minimum:





- `Step 1: Start`


- `Step 2: Execute logic`


- `Step 3: End`





Rules:





- Each logical operation MUST be bracketed by a batch step.


- The most recent batch step MUST always be tracked.


- Required steps MUST NOT be removed or renamed.


- Additional steps MAY be added as needed.





-----





### Rule P7 — SQL Execution





- SQL MUST be assigned to `vSQLCmd` before execution.


- All substantive SQL MUST be executed through logging-aware wrappers.


- Single-statement and multi-statement executions MUST be logged.


- Row counts MUST be captured when relevant.


- Direct execution of data-changing SQL inside the procedure body is NOT allowed unless explicitly requested.





-----





### Rule P8 — Batch Closure





- Only top-level procedures MAY close a batch.


- Top-level procedures MUST close the batch exactly once.


- Nested procedures MUST NEVER close the batch.





-----





### Rule P9 — Section Header Formatting





All procedures MUST use section headers in this exact format:





- Each section header is a **3-line block**:


  - Line 1: `-- ------------------------------------------------------------`


  - Line 2: `-- <N>. <Section Title>`


  - Line 3: `-- ------------------------------------------------------------`


- Exactly **one space** after `--` on each header line.


- The dashed line MUST be exactly: `-- ------------------------------------------------------------`


- Section titles MUST be numbered:


  - `0. ...` for standardized declarations


  - `1. ...` onward for procedural flow


- Each major section MUST be separated by a blank line after the header block.


- Sub-examples inside a section MUST use plain `--` comments (no dashed separators).


- Never use block comments (`/* ... */`) for sectioning.





Required canonical section titles (minimum set):





```


0. Standard logging variables (standardized names)


1. Build reproducible vProcName string for logging


2. Create or look up batch


3. Start batch step logging (proc root step)


4. Main logic section


5. End logging and close batch if top-level


```





Additional numbered sections MAY be inserted between 4 and 5 using integer numbering.





-----





### Rule P10 — Canonical Procedure Skeleton Order





1. Declare standard logging variables


1. Build `vProcName` and `vMessage`


1. Resolve `vBatchID`


1. Create procedure root batch step and required Step 1/2/3 steps


1. Execute logic only via wrappers


1. Close batch only when top-level





-----





### Rule P11 — Control Flow Formatting





- `IF / ELSEIF / ELSE / END IF` blocks follow the same tab indentation rules as CASE clauses.


- Body of each branch is indented **one tab** under the branch keyword.


- `END IF` appears at the same indentation level as `IF`.





```sql


IF condition THEN


		SET vMessage = 'branch a';


	ELSEIF other_condition THEN


		SET vMessage = 'branch b';


	ELSE


		SET vMessage = 'branch c';


	END IF;


```





-----





### Rule P12 — DECLARE Block Formatting





- All `DECLARE` statements appear together at the top of the procedure body, before any logic.


- One `DECLARE` per line.


- Group by variable type (logging variables first, then procedure-specific variables).


- Follow the standardized variable name order from Rule P2.





-----