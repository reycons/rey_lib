"""The security contract of the run-log writer, asserted against the database.

`control.p_run_log_ins` is SECURITY DEFINER: it writes run-log rows with its
owner's authority, on behalf of roles that have none on the table. Everything
about who may execute it therefore matters, and every change to it drops and
recreates it -- Postgres identifies routines by signature, and this estate
forbids overloads, so a new parameter cannot be added any other way.

**A recreate silently grants EXECUTE to PUBLIC.** That is Postgres's default for
a new routine, and it happened on 2026-09-14: the procedure gained a parameter,
the three intended grants were restored by hand, and `=X/rey_user` -- PUBLIC --
came back with them unnoticed. It was caught by comparing the whole ACL rather
than the roles that had been granted, and revoked. This is that comparison,
kept.

Skipped where no database is reachable. The catalog is read; no run-log record
is read, and none is written.
"""
from __future__ import annotations

import pytest

_ROUTINE = "p_run_log_ins"
_SCHEMA = "control"
#: The roles that may execute it, and the only ones.
_INTENDED = {"rey_user", "rey_loader_role", "rey_control_logger_role"}


def _catalog_rows(sql: str) -> list[tuple]:
    """Read one catalog question, or skip where there is no database."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover - driver absent
        pytest.skip("psycopg is not installed")
    try:
        with psycopg.connect("dbname=rey_apps", connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
    except Exception as exc:  # pragma: no cover - no database here
        pytest.skip(f"no database to read the catalog from: {exc}")


class TestTheRunLogWriterKeepsItsContract:
    def test_there_is_exactly_one_of_it(self) -> None:
        """An overload would leave callers reaching whichever the dispatcher
        resolved, and the procedure map keys by bare name."""
        rows = _catalog_rows(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace "
            f"WHERE n.nspname = '{_SCHEMA}' AND p.proname = '{_ROUTINE}'"
        )
        assert rows[0][0] == 1

    def test_it_is_owned_by_rey_user_and_defines_its_own_security(self) -> None:
        rows = _catalog_rows(
            "SELECT pg_get_userbyid(p.proowner), p.prosecdef, "
            "array_to_string(p.proconfig, ',') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = '{_SCHEMA}' AND p.proname = '{_ROUTINE}'"
        )
        owner, secdef, config = rows[0]
        assert owner == "rey_user"
        assert secdef is True
        # A SECURITY DEFINER routine without a pinned search_path resolves
        # names as the caller arranged them.
        assert config and "search_path" in config

    def test_public_may_not_execute_it(self) -> None:
        """The regression this file exists for. PUBLIC appears as an ACL entry
        with an empty grantee, which is why the whole ACL is read rather than
        the roles that were granted."""
        rows = _catalog_rows(
            "SELECT coalesce(array_to_string(p.proacl, ','), '') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = '{_SCHEMA}' AND p.proname = '{_ROUTINE}'"
        )
        acl = rows[0][0]
        assert acl, "an empty ACL means default privileges, which include PUBLIC"
        granted = {entry.split("=")[0] for entry in acl.split(",") if "=" in entry}
        assert "" not in granted, f"PUBLIC may execute it: {acl}"

    def test_only_the_intended_roles_may_execute_it(self) -> None:
        rows = _catalog_rows(
            "SELECT coalesce(array_to_string(p.proacl, ','), '') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = '{_SCHEMA}' AND p.proname = '{_ROUTINE}'"
        )
        granted = {
            entry.split("=")[0] for entry in rows[0][0].split(",") if "=" in entry
        }
        assert granted == _INTENDED

    def test_it_takes_the_classification_parameter(self) -> None:
        """68 parameters, the last of them defaulted, so callers that predate
        it still write -- as NULL, which is unclassified rather than false."""
        rows = _catalog_rows(
            "SELECT count(*), count(*) FILTER (WHERE parameter_name = "
            "'p_contains_sensitive_data') FROM information_schema.parameters "
            f"WHERE specific_schema = '{_SCHEMA}' "
            f"AND specific_name LIKE '{_ROUTINE}%'"
        )
        total, classified = rows[0]
        assert total == 68
        assert classified == 1
