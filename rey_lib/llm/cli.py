"""
Command-line interface for the LLM orchestration framework.

Provides seven subcommands matching the design contract's required CLI surface:

  run           Execute a contract against input data.
  status        Show the current state of a pipeline from its execution log.
  replay        Re-run a pipeline, skipping already-approved stages.
  approve       Approve a pending_approval execution record.
  reject        Reject a pending_approval execution record.
  cancel        Mark an execution record as cancelled.
  test-contract Validate and inspect a contract file without running it.

Entry point
-----------
Call main() or invoke as ``python -m rey_lib.llm.cli <subcommand> [options]``.

Exit codes
----------
0   Success.
1   Unexpected / orchestrator error.
2   Schema mismatch / validation failure.
3   Provider failure.
4   Record not found.
6   Configuration failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from rey_lib.llm.exceptions import (
    ConfigurationFailure,
    OrchestratorError,
    ProviderFailure,
    SchemaMismatch,
)
from rey_lib.llm.records import (
    STATUS_PENDING_APPROVAL,
    STATUS_SUCCESS,
    approve,
    cancel,
    load_all_records,
    load_latest_record,
    reject,
    store_approval,
    store_record,
)
from rey_lib.logs.log_utils import get_logger

__all__ = ["main"]

_logger = get_logger(__name__)

# Approval log is written alongside the execution log with this suffix.
_APPROVAL_SUFFIX = ".approvals.jsonl"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    """Parse arguments and dispatch to the appropriate subcommand handler."""
    parser = _build_parser()
    args   = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)

    args.func(args)


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> None:
    """Execute a contract against input data."""
    from rey_lib.llm.api import RunRequest  # noqa: PLC0415
    from rey_lib.llm.runner import run       # noqa: PLC0415

    schema: Optional[dict[str, Any]] = None
    if args.schema:
        schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))

    if args.data == "-":
        input_data: str = sys.stdin.read()
    else:
        input_data = Path(args.data).read_text(encoding="utf-8")

    try:
        response = run(RunRequest(
            pipeline_id   = args.pipeline_id,
            stage_id      = args.stage_id,
            contract_path = Path(args.contract),
            input_data    = input_data,
            provider      = args.provider,
            model         = args.model,
            max_tokens    = args.max_tokens,
            max_rows      = args.max_rows,
            output_schema = schema,
            log           = Path(args.log) if args.log else None,
        ))
    except ConfigurationFailure as exc:
        _logger.error("configuration failure: %s", exc)
        sys.exit(6)
    except ProviderFailure as exc:
        _logger.error("provider failure: %s", exc)
        sys.exit(3)
    except SchemaMismatch as exc:
        _logger.error("schema mismatch: %s", exc)
        sys.exit(2)
    except OrchestratorError as exc:
        _logger.error("orchestrator error: %s", exc)
        sys.exit(1)

    if not args.quiet and response.parsed_response:
        sys.stdout.write(
            json.dumps(response.parsed_response, indent=2, default=str) + "\n"
        )

    sys.exit(0 if response.status == STATUS_SUCCESS else 1)


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> None:
    """Show the current state of a pipeline from its JSONL execution log."""
    log = Path(args.log)
    records = load_all_records(log)

    if not records:
        sys.stdout.write(f"No records found in {log}\n")
        sys.exit(0)

    if args.pipeline_id:
        records = [r for r in records if r.pipeline_id == args.pipeline_id]
        if not records:
            sys.stdout.write(
                f"No records for pipeline '{args.pipeline_id}' in {log}\n"
            )
            sys.exit(4)

    # Show latest record per (pipeline_id, stage_id).
    seen: dict[tuple[str, str], Any] = {}
    for record in records:
        seen[(record.pipeline_id, record.stage_id)] = record

    rows = sorted(seen.values(), key=lambda r: (r.pipeline_id, r.stage_id))
    header = f"{'PIPELINE':<30}  {'STAGE':<20}  {'STATUS':<20}  {'RUN_ID':<38}  ELAPSED_MS"
    sys.stdout.write(header + "\n")
    sys.stdout.write("-" * len(header) + "\n")
    for r in rows:
        sys.stdout.write(
            f"{r.pipeline_id:<30}  {r.stage_id:<20}  {r.status:<20}  "
            f"{r.run_id:<38}  {r.elapsed_ms}\n"
        )

    sys.exit(0)


# ---------------------------------------------------------------------------
# Subcommand: replay
# ---------------------------------------------------------------------------

def _cmd_replay(args: argparse.Namespace) -> None:
    """Re-run a pipeline, skipping stages already approved in the log."""
    from rey_lib.llm.pipeline import Pipeline, Stage  # noqa: PLC0415

    log = Path(args.log)
    if not log.exists():
        sys.stderr.write(f"Log not found: {log}\n")
        sys.exit(4)

    # Reconstruct a minimal pipeline from the contract paths supplied.
    # For full replay, the caller must supply the same stage/contract mapping.
    if not args.stages:
        sys.stderr.write(
            "replay requires --stage <stage_id:contract_path> pairs.\n"
            "Example: --stage extract:contracts/extract.md "
            "--stage classify:contracts/classify.md\n"
        )
        sys.exit(6)

    stages: list[Stage] = []
    for spec in args.stages:
        if ":" not in spec:
            sys.stderr.write(f"Invalid --stage spec '{spec}'. Expected stage_id:contract_path\n")
            sys.exit(6)
        stage_id, contract = spec.split(":", 1)
        stages.append(Stage(
            stage_id      = stage_id.strip(),
            contract_path = Path(contract.strip()),
            max_tokens    = args.max_tokens,
            max_rows      = args.max_rows,
        ))

    pipeline = Pipeline(
        stages   = stages,
        log      = log,
        provider = args.provider,
        model    = args.model,
    )

    try:
        responses = pipeline.resume(
            initial_data = args.input_data or "",
            pipeline_id  = args.pipeline_id,
        )
    except OrchestratorError as exc:
        _logger.error("replay failed: %s", exc)
        sys.exit(1)

    for resp in responses:
        sys.stdout.write(f"  stage: {resp.run_id}  status: {resp.status}\n")

    all_ok = all(r.status in (STATUS_SUCCESS, STATUS_PENDING_APPROVAL) for r in responses)
    sys.exit(0 if all_ok else 1)


# ---------------------------------------------------------------------------
# Subcommand: approve
# ---------------------------------------------------------------------------

def _cmd_approve(args: argparse.Namespace) -> None:
    """Approve a pending_approval execution record."""
    log    = Path(args.log)
    record = _find_record(log, args.run_id)

    if record.status != STATUS_PENDING_APPROVAL:
        sys.stderr.write(
            f"Record {args.run_id} has status '{record.status}', "
            "not 'pending_approval'.\n"
        )
        sys.exit(1)

    updated, approval = approve(record, reviewer=args.reviewer, comments=args.comments or "")
    store_record(updated, log)
    store_approval(approval, Path(str(log) + _APPROVAL_SUFFIX))

    sys.stdout.write(
        f"Approved run_id={updated.run_id} stage={updated.stage_id} "
        f"by {args.reviewer} (approval_id={approval.approval_id})\n"
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Subcommand: reject
# ---------------------------------------------------------------------------

def _cmd_reject(args: argparse.Namespace) -> None:
    """Reject a pending_approval execution record."""
    log    = Path(args.log)
    record = _find_record(log, args.run_id)

    if record.status != STATUS_PENDING_APPROVAL:
        sys.stderr.write(
            f"Record {args.run_id} has status '{record.status}', "
            "not 'pending_approval'.\n"
        )
        sys.exit(1)

    updated, approval = reject(record, reviewer=args.reviewer, comments=args.comments or "")
    store_record(updated, log)
    store_approval(approval, Path(str(log) + _APPROVAL_SUFFIX))

    sys.stdout.write(
        f"Rejected run_id={updated.run_id} stage={updated.stage_id} "
        f"by {args.reviewer} (approval_id={approval.approval_id})\n"
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Subcommand: cancel
# ---------------------------------------------------------------------------

def _cmd_cancel(args: argparse.Namespace) -> None:
    """Mark an execution record as cancelled."""
    log    = Path(args.log)
    record = _find_record(log, args.run_id)

    updated = cancel(record, reason=args.reason or "")
    store_record(updated, log)

    sys.stdout.write(f"Cancelled run_id={updated.run_id} stage={updated.stage_id}\n")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Subcommand: test-contract
# ---------------------------------------------------------------------------

def _cmd_test_contract(args: argparse.Namespace) -> None:
    """Validate and inspect a contract file without executing it."""
    from rey_lib.llm.contract import load as load_contract  # noqa: PLC0415

    from rey_lib.errors.error_utils import ConfigError  # noqa: PLC0415

    try:
        contract = load_contract(Path(args.contract))
    except (ConfigError, OSError) as exc:
        sys.stderr.write(f"Contract validation failed: {exc}\n")
        sys.exit(6)

    sys.stdout.write(
        f"Contract:       {contract.name}\n"
        f"Version:        {contract.version}\n"
        f"Effective date: {contract.effective_date}\n"
        f"Body length:    {len(contract.body)} chars\n"
        f"Hash (SHA-256): {contract.hash}\n"
        f"Path:           {contract.path}\n"
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _find_record(log: Path, run_id: str) -> Any:
    """Load and return a record by run_id, exiting with code 4 if not found."""
    records = load_all_records(log)
    for record in reversed(records):
        if record.run_id == run_id:
            return record
    sys.stderr.write(f"Record not found: run_id={run_id} in {log}\n")
    sys.exit(4)


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog        = "rey_lib.llm",
        description = "LLM orchestration framework CLI.",
    )
    sub = parser.add_subparsers(title="subcommands", metavar="<command>")

    # ---- run ----------------------------------------------------------------
    p_run = sub.add_parser("run", help="Execute a contract against input data.")
    p_run.add_argument("--data",        required=True,  help="Input file path or '-' for stdin.")
    p_run.add_argument("--contract",    required=True,  help="Contract markdown file.")
    p_run.add_argument("--pipeline-id", default="cli",  dest="pipeline_id")
    p_run.add_argument("--stage-id",    default="run",  dest="stage_id")
    p_run.add_argument("--provider",    default="",     help="Provider name (overrides LLM_PROVIDER env var).")
    p_run.add_argument("--model",       default="",     help="Model identifier (overrides LLM_MODEL env var).")
    p_run.add_argument("--max-tokens",  default=4000,   dest="max_tokens",  type=int)
    p_run.add_argument("--max-rows",    default=200,    dest="max_rows",    type=int)
    p_run.add_argument("--schema",      default=None,   help="JSON Schema file for output validation.")
    p_run.add_argument("--log",         default=None,   help="JSONL execution log path.")
    p_run.add_argument("--quiet",       action="store_true")
    p_run.set_defaults(func=_cmd_run)

    # ---- status -------------------------------------------------------------
    p_status = sub.add_parser("status", help="Show pipeline state from the execution log.")
    p_status.add_argument("--log",         required=True, help="JSONL execution log path.")
    p_status.add_argument("--pipeline-id", default="",   dest="pipeline_id",
                          help="Filter to a specific pipeline. Omit to show all.")
    p_status.set_defaults(func=_cmd_status)

    # ---- replay -------------------------------------------------------------
    p_replay = sub.add_parser("replay", help="Re-run a pipeline, skipping approved stages.")
    p_replay.add_argument("--log",         required=True,  help="JSONL execution log path.")
    p_replay.add_argument("--pipeline-id", required=True,  dest="pipeline_id")
    p_replay.add_argument("--stage",       action="append", dest="stages",
                          metavar="stage_id:contract_path",
                          help="Stage definition (repeatable, in order).")
    p_replay.add_argument("--input-data",  default="",     dest="input_data",
                          help="Initial input text (overrides log-derived input).")
    p_replay.add_argument("--provider",    default="")
    p_replay.add_argument("--model",       default="")
    p_replay.add_argument("--max-tokens",  default=4000,   dest="max_tokens",  type=int)
    p_replay.add_argument("--max-rows",    default=200,    dest="max_rows",    type=int)
    p_replay.set_defaults(func=_cmd_replay)

    # ---- approve ------------------------------------------------------------
    p_approve = sub.add_parser("approve", help="Approve a pending_approval record.")
    p_approve.add_argument("--log",      required=True, help="JSONL execution log path.")
    p_approve.add_argument("--run-id",   required=True, dest="run_id")
    p_approve.add_argument("--reviewer", required=True)
    p_approve.add_argument("--comments", default="")
    p_approve.set_defaults(func=_cmd_approve)

    # ---- reject -------------------------------------------------------------
    p_reject = sub.add_parser("reject", help="Reject a pending_approval record.")
    p_reject.add_argument("--log",      required=True, help="JSONL execution log path.")
    p_reject.add_argument("--run-id",   required=True, dest="run_id")
    p_reject.add_argument("--reviewer", required=True)
    p_reject.add_argument("--comments", default="")
    p_reject.set_defaults(func=_cmd_reject)

    # ---- cancel -------------------------------------------------------------
    p_cancel = sub.add_parser("cancel", help="Mark an execution record as cancelled.")
    p_cancel.add_argument("--log",    required=True, help="JSONL execution log path.")
    p_cancel.add_argument("--run-id", required=True, dest="run_id")
    p_cancel.add_argument("--reason", default="")
    p_cancel.set_defaults(func=_cmd_cancel)

    # ---- test-contract ------------------------------------------------------
    p_tc = sub.add_parser("test-contract", help="Validate and inspect a contract file.")
    p_tc.add_argument("--contract", required=True, help="Contract markdown file.")
    p_tc.set_defaults(func=_cmd_test_contract)

    return parser


if __name__ == "__main__":
    main()
