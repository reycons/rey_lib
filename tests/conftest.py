"""Shared fixtures for tests that write run-log records.

Run logging is owned by ``RunLog``. Tests that write records take one, rather
than constructing a context by hand and relying on logging to read fields off
it — that fixture pattern is what produced the ctx-shaped write API in the
first place, so repairing it would re-cement what this migration removes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.logs.run_log import RunLog
from rey_lib.run import establish_run_identity

_NEXT_TEST_RUN_ID = [1]


def start_test_run(ctx: Any, run_id: int | None = None) -> Any:
    """Give ``ctx`` the identity a launched run would carry.

    In production identity comes from the manifest: ``Run.start`` inserts the
    row, the database generates ``run_manifest_id``, and the application carries
    it as ``run_id``. ``establish_run_identity`` only adds the display and
    filing timestamps.

    A test that needs a launched-looking context wants both halves without a
    database, so this supplies the id the manifest would have generated and then
    calls the real timestamp step. Ids are distinct across calls for the same
    reason real ones are: two runs are two runs.
    """
    if run_id is None:
        run_id = getattr(ctx, "run_id", None)
    if run_id is None:
        run_id = _NEXT_TEST_RUN_ID[0]
        _NEXT_TEST_RUN_ID[0] += 1
    ctx.run_id = run_id
    establish_run_identity(ctx)
    return ctx


def make_run_log(
    tmp_path: Path | str,
    *,
    app: str = "rey_loader",
    run_id: str = "00000000-0000-4000-8000-000000000001",
    run_timestamp: str = "20260822_000000",
    destination: str = "jsonl",
    control: Any = None,
    workflow: str | None = None,
    pipeline: str | None = None,
    path: str | None = None,
) -> RunLog:
    """Build a RunLog writing into ``tmp_path``.

    ``path`` supplies an already-resolved run log, which is how a second writer
    joins an existing run — the cross-process continuation case.
    """
    return RunLog(
        app=app,
        run_id=run_id,
        run_timestamp=run_timestamp,
        log_dir=None if path else str(tmp_path),
        path=path,
        destination=destination,
        control=control,
        workflow=workflow,
        pipeline=pipeline,
    )


#: The columns ``control.update_file_manifest`` writes.
#:
#: The routine names these and drops anything else, so a caller handing it a
#: field the table has no column for -- ``classification``, which is an event
#: rather than manifest state -- changes nothing. Mirrored here so a call the
#: database would ignore is ignored in a test too.
_UPDATABLE_FILE_COLUMNS = (
    "path", "file_name", "base_name", "file_extension",
    "checksum_sha256", "size_bytes", "source_name", "evidence", "producer",
    "data_profile_key",
)


class MintingControl:
    """A Control that mints run_log_ids the way the control database does.

    A run log's parenting is database identity: a record's parent is the id its
    parent row was given, and the database gives it. A test that wants a
    hierarchy therefore needs a database to mint one, and this stands in for
    it -- returning sequential ids and keeping every row it was asked to write.
    """

    def __init__(self) -> None:
        self.owns_batch = False
        self.batch_id: Any = None
        self.batch_step_id: Any = None
        self.run_log: Any = None
        self.rows: list[dict[str, Any]] = []

    # The batch surface a run log opens and closes around its work. Held as
    # state rather than recorded as calls: what these tests assert on is the
    # identity a record was given, not the batch machinery around it.
    def start_batch(self, batch_name: Any = None, required: bool = False,
                    **kw: Any) -> int:
        self.batch_id = 1
        self.owns_batch = True
        return self.batch_id

    def end_batch(self, status: Any = None, required: bool = False,
                  **kw: Any) -> None:
        self.owns_batch = False

    def start_step(self, step_name: Any = None, required: bool = False,
                   **kw: Any) -> int:
        self.batch_step_id = (self.batch_step_id or 0) + 1
        return self.batch_step_id

    def end_step(self, status: Any = None, required: bool = False,
                 **kw: Any) -> None:
        self.batch_step_id = None

    def write_run_log_record(self, *, required: bool = False, **values: Any) -> int:
        run_log_id = len(self.rows) + 1
        self.rows.append({"run_log_id": run_log_id, **values})
        return run_log_id


class ControlDouble(MintingControl):
    """The control database's governed-file surface, in memory.

    The manifest is a table now, so a test that exercises governed files needs
    something that behaves like one: it mints identities, keeps a file's
    mutations in the order they were written, and answers reads with the same
    shapes the routines return. It is not a stub that records calls -- the
    behaviour under test depends on what comes back.

    Integration against the real database is a separate concern and stays in
    the tests that talk to it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[int, dict[str, Any]] = {}
        self.mutations: list[dict[str, Any]] = []

    # -- writes -------------------------------------------------------------

    def inventory_file(self, path: str, file_name: str, base_name: str,
                       file_extension: str, checksum_sha256: str,
                       size_bytes: int, source_name: Any = None,
                       evidence: Any = None, producer: Any = None,
                       required: bool = True) -> int:
        file_manifest_id = len(self.files) + 1
        self.files[file_manifest_id] = {
            "file_manifest_id": file_manifest_id, "path": path,
            "file_name": file_name, "base_name": base_name,
            "file_extension": file_extension,
            "checksum_sha256": checksum_sha256, "size_bytes": size_bytes,
            "source_name": source_name, "evidence": evidence,
            "producer": producer,
        }
        return file_manifest_id

    def update_file_manifest(self, file_manifest_id: int, required: bool = True,
                             **fields: Any) -> None:
        """Change only what the routine can change; anything else is dropped."""
        row = self.files.get(int(file_manifest_id))
        if row is not None:
            row.update({
                key: value for key, value in fields.items()
                if value is not None and key in _UPDATABLE_FILE_COLUMNS
            })

    def append_file_mutation(self, file_manifest_id: int, record_type: str,
                             action: str, status: Any = None,
                             source_record_id: Any = None,
                             run_log_id: Any = None, path: Any = None,
                             required: bool = True, **fields: Any) -> int:
        file_mutation_id = len(self.mutations) + 1
        self.mutations.append({
            "file_mutation_id": file_mutation_id,
            "file_manifest_id": int(file_manifest_id),
            "record_type": record_type, "action": action, "status": status,
            "source_record_id": source_record_id, "run_log_id": run_log_id,
            "path": path, "batch_step_id": self.batch_step_id,
            "deleted_in": None, **fields,
        })
        # The manifest tracks current location, as the database does.
        if path:
            self.update_file_manifest(file_manifest_id, path=path)
        return file_mutation_id

    # -- reads --------------------------------------------------------------

    def get_file_manifest(self, file_manifest_id: int,
                          required: bool = True) -> Any:
        return self.files.get(int(file_manifest_id))

    def list_file_manifest(self, required: bool = True) -> list[dict[str, Any]]:
        return [self.files[k] for k in sorted(self.files)]

    def find_file_manifest(self, required: bool = True,
                           limit: int = 100, **filters: Any) -> list[dict[str, Any]]:
        wanted = {k: v for k, v in filters.items() if v is not None}
        return [row for row in self.list_file_manifest()
                if all(row.get(k) == v for k, v in wanted.items())][:limit]

    def list_file_mutations(self, file_manifest_id: Any = None,
                            required: bool = True) -> list[dict[str, Any]]:
        if file_manifest_id is None:
            return list(self.mutations)
        return [m for m in self.mutations
                if m["file_manifest_id"] == int(file_manifest_id)]

    def file_history(self, file_manifest_id: int,
                     required: bool = True) -> list[dict[str, Any]]:
        return self.list_file_mutations(file_manifest_id)

    def files_for_run(self, run_id: int,
                      required: bool = True) -> list[dict[str, Any]]:
        """The files one run recorded, by the run's durable identity."""
        run_log_ids = {row["run_log_id"] for row in self.rows
                       if row.get("run_id") == run_id}
        touched = {m["file_manifest_id"] for m in self.mutations
                   if m.get("run_log_id") in run_log_ids}
        return [self.files[k] for k in sorted(touched) if k in self.files]

    def find_files(self, file_extensions: Any = None, classified: Any = None,
                   action: Any = None, status: Any = None,
                   current_only: bool = True, converted: Any = None,
                   required: bool = True) -> list[dict[str, Any]]:
        """The control.file_vw selection, in memory.

        One row per mutation carrying the file it mutates, filtered the way the
        routine filters, with the current row being the latest surviving one.
        """
        wanted = {str(e).lower() for e in (file_extensions or [])}
        rows: list[dict[str, Any]] = []
        for file_manifest_id in sorted(self.files):
            file_row = self.files[file_manifest_id]
            if wanted and str(file_row.get("file_extension") or "").lower() not in wanted:
                continue
            # Classification is an event, so being classified is a question
            # about this file's history rather than a column on its row.
            is_classified = any(
                mutation.get("record_type") == "source_file_classification"
                for mutation in self.list_file_mutations(file_manifest_id)
            )
            if classified is not None and is_classified != classified:
                continue
            history = [m for m in self.list_file_mutations(file_manifest_id)
                       if m.get("deleted_in") is None]
            is_converted = any(m.get("conversion") for m in history)
            if converted is not None and is_converted != converted:
                continue
            chosen = history[-1:] if current_only else history
            for mutation in chosen:
                if action is not None and mutation.get("action") != action:
                    continue
                if status is not None and mutation.get("status") != status:
                    continue
                rows.append({
                    **{k: v for k, v in file_row.items() if k != "path"},
                    **mutation,
                    "path": mutation.get("path") or file_row.get("path"),
                    "manifest_path": file_row.get("path"),
                    "is_classified": is_classified,
                    "is_converted": is_converted,
                })
        return rows


def make_db_run_log(tmp_path: Path | str, **kwargs: Any) -> RunLog:
    """A run log writing to both destinations, with ids minted for it.

    This is how production runs: `run_store: both`, so records carry the
    identity the database gave them and the JSONL serializes it.
    """
    kwargs.setdefault("destination", "both")
    kwargs.setdefault("control", MintingControl())
    return make_run_log(tmp_path, **kwargs)


@pytest.fixture()
def run_log(tmp_path: Path) -> RunLog:
    """A JSONL run log writing into the test's tmp_path."""
    return make_run_log(tmp_path)


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the launch boundary start a run without a control database.

    Recording the run is what creates its identity, so ``build_ctx_for_app``
    reaches the control database on every launch. A test about what the
    bootstrap does *around* that -- starting logging, installing the error
    boundary, collecting shared objects -- should not need a database standing
    up to say so.

    Patches the creation only. Everything after it, including the ordering that
    puts the run before logging, runs exactly as it does in production.
    """
    from rey_lib.config import bootstrap
    from rey_lib.run import Run

    def _start(control: Any, **kwargs: Any) -> Run:
        run_id = _NEXT_TEST_RUN_ID[0]
        _NEXT_TEST_RUN_ID[0] += 1
        return Run(run_id=run_id, control=control, **{
            k: v for k, v in kwargs.items() if k != "control"})

    monkeypatch.setattr(bootstrap, "_open_control", lambda ctx: object())
    monkeypatch.setattr(bootstrap.Run, "start", staticmethod(_start))


@pytest.fixture(autouse=True)
def _own_no_connections_between_tests() -> Any:
    """Give every test a runtime holding no connections.

    Connection objects belong to the runtime, not to a context, which is what
    lets any context identifying a configured connection reach the same object.
    Under test that same property makes one test's connections visible to the
    next, so the runtime is emptied between them -- the equivalent of a fresh
    process, which is what each test is pretending to be.
    """
    from rey_lib.db.connection import connection_owner

    connection_owner().close()
    yield
    connection_owner().close()
