"""Reading a routine's body when its signature defeats the parser.

``parse_plpgsql`` resolves a routine's RETURN TYPE, and libpg_query -- parsing
outside a running server -- refuses any schema but ``pg_catalog`` and
``public``. A routine declared ``RETURNS SETOF control.file_manifest`` therefore
could not be read at all, while the body beneath it was perfectly ordinary.

That mattered more than one routine's worth. The batch-step conversion turns
``LANGUAGE sql`` routines into ``plpgsql`` so they can record their own step,
and ``parse_sql`` never resolves a return type while ``parse_plpgsql`` always
does. Nine sibling routines returning the same composite parse today only
because they have not been converted yet.

These use definition strings rather than a database: the subject is what the
parser accepts, and the parser needs no connection.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pglast",
    reason="routine-body analysis needs the rey_lib[db_references] extra",
)

from rey_lib.db.postgres_references import (  # noqa: E402
    _definition_readings,
    analyse_routine,
)


def _setof_composite(returns: str = "control.file_manifest") -> str:
    """A plpgsql routine shaped like control.f_file_manifest_get."""
    return f"""CREATE OR REPLACE FUNCTION control.f_thing_get(p_id bigint)
 RETURNS SETOF {returns}
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'control', 'pg_temp'
AS $function$
BEGIN
    RETURN QUERY
        SELECT  m.*
        FROM    control.file_manifest AS m
        WHERE   m.file_manifest_id = p_id;
END;
$function$"""


class TestASchemaQualifiedCompositeReturn:
    """The signature is the obstacle, and the body is not."""

    def test_the_body_is_read_despite_a_return_type_the_parser_cannot_resolve(
        self,
    ) -> None:
        """This is the whole point: complete, with the read it performs."""
        analysis = analyse_routine(_setof_composite(), "plpgsql")

        assert analysis.status == "complete"
        assert "control.file_manifest" in analysis.reads
        assert analysis.gaps == []

    def test_the_definition_as_written_is_genuinely_unparseable(self) -> None:
        """Without the fallback there is nothing to report -- not a weaker answer.

        Asserting the premise, so this suite still means something if libpg_query
        ever learns to resolve other schemas: the second reading would then be
        unnecessary rather than load-bearing, and this is where that shows.
        """
        from pglast import parse_plpgsql

        with pytest.raises(Exception) as raised:
            parse_plpgsql(_setof_composite())
        assert "LookupExplicitNamespace" in str(raised.value)

    def test_the_substitution_keeps_the_return_a_set(self) -> None:
        """RETURNS record would be rejected by plpgsql, not merely unhelpful.

        A body using RETURN QUERY is refused outright by a non-SETOF signature
        -- "cannot use RETURN QUERY in a non-SETOF function" -- so set-ness has
        to survive. The substituted reading is asserted directly because a
        weaker one still parses SOME routines and would fail only on these.
        """
        readings = _definition_readings(_setof_composite())

        assert len(readings) == 2
        assert "RETURNS SETOF record" in readings[1]
        assert "RETURNS SETOF control.file_manifest" not in readings[1]

    def test_a_definition_that_parses_is_offered_one_reading(self) -> None:
        """Nothing that works today takes a different route."""
        plain = """CREATE OR REPLACE FUNCTION control.f_thing(p_id bigint)
 RETURNS TABLE(thing jsonb)
 LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN QUERY SELECT t.thing FROM control.file_manifest AS t
    WHERE t.file_manifest_id = p_id;
END;
$function$"""

        assert _definition_readings(plain) == (plain,)
        assert analyse_routine(plain, "plpgsql").status == "complete"

    def test_only_the_signature_is_rewritten_never_the_body(self) -> None:
        """A body may legitimately contain that text, in SQL it builds.

        The substitution takes the FIRST match, so applied to the whole
        definition it could reach a literal. It is confined to the signature,
        which is the only part the parser objects to.
        """
        definition = """CREATE OR REPLACE FUNCTION control.f_thing_get(p_id bigint)
 RETURNS SETOF control.file_manifest
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_ddl TEXT;
BEGIN
    v_ddl := 'RETURNS SETOF control.file_manifest';
    RETURN QUERY SELECT m.* FROM control.file_manifest AS m
    WHERE m.file_manifest_id = p_id;
END;
$function$"""

        substituted = _definition_readings(definition)[1]

        # The signature moved; the literal in the body did not.
        assert "RETURNS SETOF record" in substituted
        assert substituted.count("RETURNS SETOF control.file_manifest") == 1
        assert "v_ddl := 'RETURNS SETOF control.file_manifest';" in substituted

    def test_an_unreadable_body_is_still_reported_unparsed(self) -> None:
        """The invariant the fallback must not weaken.

        A second reading exists to remove an irrelevant obstacle, never to
        accept a body that cannot be read.
        """
        broken = """CREATE OR REPLACE FUNCTION control.f_thing_get(p_id bigint)
 RETURNS SETOF control.file_manifest
 LANGUAGE plpgsql
AS $function$
BEGIN
    this is not plpgsql at all (((
END;
$function$"""

        assert analyse_routine(broken, "plpgsql").status == "unparsed"
