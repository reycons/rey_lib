"""
Tests for JsonlHandler diagnostics-aware ctx_dump behavior.

Covers:
1. No diagnostics config — legacy flat ctx_fields behavior unchanged.
2. diagnostics.error.dump_ctx = true — ctx_dump present with whitelisted fields only.
3. diagnostics.error.dump_ctx = false — no ctx_dump, event fields still logged.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.logs.jsonl_handler import JsonlHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(**attrs: Any) -> SimpleNamespace:
    """Return a minimal ctx-like object with the given attributes."""
    return SimpleNamespace(**attrs)


def _make_diag(level: str, **flags: Any) -> SimpleNamespace:
    """Build a minimal ctx with ctx.diagnostics.{level} configured."""
    level_ns  = SimpleNamespace(**flags)
    diag_ns   = SimpleNamespace(**{level: level_ns})
    return _make_ctx(diagnostics=diag_ns, batch_id=42, env="test", secret="s3cr3t")


def _emit_and_read(
    handler: JsonlHandler,
    message: str = "test message",
    level: int = logging.ERROR,
    extra: dict | None = None,
) -> dict:
    """Emit one log record through handler and return the parsed JSONL dict."""
    logger = logging.getLogger(f"test.{id(handler)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False

    logger.log(level, message, extra=extra or {})

    handler.close()
    path = Path(handler._fh.name) if not handler._fh.closed else _find_jsonl(handler)
    return json.loads(path.read_text(encoding="utf-8").strip())


def _make_handler(tmp_path: Path, ctx: Any, ctx_fields: tuple = ()) -> tuple[JsonlHandler, Path]:
    """Return (handler, jsonl_path) writing to a temp file."""
    p = tmp_path / "test.jsonl"
    h = JsonlHandler(jsonl_path=p, context={}, ctx=ctx, ctx_fields=ctx_fields)
    return h, p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLegacyFlatBehavior:
    """Scenario 1 — no diagnostics config, constructor ctx_fields used flat."""

    def test_flat_fields_written_to_record(self, tmp_path: Path) -> None:
        """ctx_fields from constructor are written as flat top-level keys."""
        ctx = _make_ctx(batch_id=99, env="dev")
        handler, path = _make_handler(tmp_path, ctx, ctx_fields=("batch_id", "env"))

        logger = logging.getLogger(f"test.legacy.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("legacy test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["batch_id"] == 99
        assert record["env"] == "dev"
        assert "ctx_dump" not in record

    def test_missing_ctx_attr_writes_none(self, tmp_path: Path) -> None:
        """A ctx_field that doesn't exist on ctx writes None without raising."""
        ctx = _make_ctx(batch_id=1)
        handler, path = _make_handler(tmp_path, ctx, ctx_fields=("batch_id", "missing_attr"))

        logger = logging.getLogger(f"test.missing.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("missing attr test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["batch_id"] == 1
        assert record["missing_attr"] is None


class TestDumpCtxTrue:
    """Scenario 2 — diagnostics.error.dump_ctx = true."""

    def test_ctx_dump_contains_whitelisted_fields(self, tmp_path: Path) -> None:
        """ctx_dump is a nested dict with only the fields listed in ctx_fields."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id", "env"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.dumpctx.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("dump ctx test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" in record
        assert record["ctx_dump"]["batch_id"] == 42
        assert record["ctx_dump"]["env"] == "test"
        assert "secret" not in record["ctx_dump"]

    def test_ctx_dump_excludes_none_values(self, tmp_path: Path) -> None:
        """Fields that are None on ctx are excluded from ctx_dump."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id", "destination_table"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        # destination_table not set on ctx
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.none.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("none field test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" in record
        assert "destination_table" not in record["ctx_dump"]

    def test_event_fields_still_present(self, tmp_path: Path) -> None:
        """operation and other extra fields appear alongside ctx_dump."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.eventfields.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("event fields test", extra={"operation": "REJECT", "source_path": "/tmp/f.csv"})
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["operation"] == "REJECT"
        assert record["source_path"] == "/tmp/f.csv"
        assert "ctx_dump" in record


class TestDotPathResolution:
    """Dot-separated paths in ctx_fields are walked generically."""

    def test_dot_path_resolved_into_ctx_dump(self, tmp_path: Path) -> None:
        """A path like 'loads.load.destination_table' is walked on ctx."""
        load_ns  = SimpleNamespace(destination_table="NaviStage.Advantage_SCH.transaction")
        loads_ns = SimpleNamespace(name="advantage_transactions", load=load_ns)
        ctx = SimpleNamespace(
            loads=loads_ns,
            batch_id=1,
            diagnostics=SimpleNamespace(
                error=SimpleNamespace(
                    enabled=True,
                    dump_ctx=True,
                    ctx_fields=["loads.name", "loads.load.destination_table"],
                    dump_stack_trace=False,
                    dump_sql=True,
                )
            ),
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.dotpath.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("dot path test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["ctx_dump"]["loads.name"] == "advantage_transactions"
        assert record["ctx_dump"]["loads.load.destination_table"] == "NaviStage.Advantage_SCH.transaction"

    def test_missing_segment_excluded(self, tmp_path: Path) -> None:
        """A path whose intermediate segment is None is excluded from ctx_dump."""
        ctx = SimpleNamespace(
            loads=None,
            diagnostics=SimpleNamespace(
                error=SimpleNamespace(
                    enabled=True,
                    dump_ctx=True,
                    ctx_fields=["loads.load.destination_table"],
                    dump_stack_trace=False,
                    dump_sql=True,
                )
            ),
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.missing_seg.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("missing segment test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record.get("ctx_dump", {}).get("loads.load.destination_table") is None


class TestDumpCtxFalse:
    """Scenario 3 — diagnostics.error.dump_ctx = false."""

    def test_no_ctx_dump_in_record(self, tmp_path: Path) -> None:
        """ctx_dump is absent when dump_ctx is false."""
        ctx = _make_diag(
            "error",
            dump_ctx=False,
            ctx_fields=["batch_id", "env"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.nodump.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("no dump test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" not in record

    def test_event_fields_still_logged(self, tmp_path: Path) -> None:
        """Event-level extra fields are present even when dump_ctx is false."""
        ctx = _make_diag(
            "error",
            dump_ctx=False,
            ctx_fields=["batch_id"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.eventsonly.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("event only", extra={"operation": "LOAD", "rows": 500})
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["operation"] == "LOAD"
        assert record["rows"] == 500
        assert "ctx_dump" not in record
