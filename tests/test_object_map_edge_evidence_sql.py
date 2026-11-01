"""Actual edge evidence SQL, with only TEMP fixtures and an explicit test connection."""
import os
import re

import pytest
from rey_lib.inferred_architecture.object_map import _SQL_DIR

pytestmark = pytest.mark.skipif(not os.environ.get("REY_TEST_POSTGRES_DSN"),
                                reason="Requires an explicit PostgreSQL TEMP-fixture connection")


def test_only_canonical_region_call_sites_support_the_selected_link():
    psycopg = pytest.importorskip("psycopg")
    sql = (_SQL_DIR / "object_map_edge_evidence.sql").read_text()
    tables = {
        "object_map_relationship_vw": "canonical_object_id bigint, linked_canonical_object_id bigint, entry_code_symbol_ids bigint[]",
        "canonical_region_vw": "canonical_object_id bigint, role text, target_code_symbol_id bigint, target_kind text, target_code_file_id bigint",
        "relationship_vw": "code_relationship_id bigint, code_edge_id bigint, repository_key text, relative_path text, source_line integer, source_column integer, source_identity text, target_identity text, edge_kind text, resolution text, source_code_symbol_id bigint, target_code_symbol_id bigint, code_file_id bigint, is_test boolean, is_resolved boolean",
    }
    connection = psycopg.connect(os.environ["REY_TEST_POSTGRES_DSN"])
    try:
        with connection.cursor() as cursor:
            for name, columns in tables.items():
                cursor.execute(f"CREATE TEMP TABLE evidence_{name} ({columns}) ON COMMIT DROP")
                sql = sql.replace("code." + name, "pg_temp.evidence_" + name)
            cursor.execute("INSERT INTO pg_temp.evidence_object_map_relationship_vw VALUES (1,2,ARRAY[20]),(3,2,ARRAY[20])")
            cursor.execute("INSERT INTO pg_temp.evidence_canonical_region_vw VALUES "
                           "(1,'owner',10,'method',50),(1,'owner',10,'method',50),"
                           "(1,'exclusive',11,'method',51),(1,'linked_canonical',20,'method',52),"
                           "(1,'exclusive',NULL,'module',53),(3,'owner',99,'method',99)")

            def site(number, source, file=50, kind="call", resolved=True, test=False, target=20):
                return (number, number + 100, "app", f"site_{number}.py", 42, 0,
                        "Owner.method", "Target.method", kind, "resolved", source, target, file, test, resolved)

            rows = [site(1, 10), site(2, 11, 51, "internal_call"), site(3, 99, 99),
                    site(4, 20, 52), site(5, 10, resolved=False), site(6, 10, test=True),
                    site(7, 10, kind="import"), site(8, None, 53, "property_access"),
                    site(9, None, 99), site(10, 10, target=21)]
            cursor.executemany("INSERT INTO pg_temp.evidence_relationship_vw VALUES (" +
                               ",".join(["%s"] * 15) + ")", rows)
            sql = re.sub(r"(?<!:):(\w+)", r"%(\1)s", sql)
            cursor.execute(sql, {"sources": 1, "targets": 2})
            names = [column.name for column in cursor.description]
            found = [dict(zip(names, row)) for row in cursor.fetchall()]
            assert [row["code_relationship_id"] for row in found] == [1, 2, 8]
            assert all(row["source_line"] == 42 for row in found)
            cursor.execute(sql, {"sources": 3, "targets": 2})
            assert [row[names.index("code_relationship_id")] for row in cursor.fetchall()] == [3]
            cursor.execute(sql, {"sources": 1, "targets": 99})
            assert cursor.fetchall() == []
    finally:
        connection.rollback()
        connection.close()
