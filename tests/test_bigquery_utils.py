"""The BigQuery provider answers DBAdapter's contract through a client stand-in (backlog 1365).

No network and no credential: ``bigquery.Client`` is replaced by a stand-in that
records what it was asked and answers in the client's own shapes. The DB-API
cursor that executes queries is the library's real one.

Proved:
- a connection is opened from project, database and the optional
  maximum_bytes_billed, which reaches the client's default query job config;
- an ADC or API failure is reported as its class and HTTP status, never its message;
- datasets are listed alone, and one dataset's tables and views only when asked;
- a table's columns and existence are read from the client's table resource;
- a bounded read returns at most its limit through DBAdapter's existing DB-API path;
- a table opens on its project-qualified reference;
- a Workbench page is planned by a dry run, then read with one row beyond it,
  its filter values bound and its identifiers quoted;
- the write operations the provider does not implement are refused by name.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import google.auth.exceptions
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

from rey_lib.db import bigquery_utils
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import (
    ConfigError,
    DatabaseError,
    UnsupportedDatabaseCapabilityError,
)

DATA_PROJECT = "public-data"
EXECUTION_PROJECT = "execution-project"
#: What the stand-in is asked to run; it answers the same rows whatever it is.
QUERY = "SELECT 1"
FIELDS = [
    bigquery.SchemaField("state", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("year", "INTEGER", mode="REQUIRED", default_value_expression="1910"),
]


class _Rows:
    """A query result in the shape the DB-API cursor reads."""

    num_dml_affected_rows = None

    def __init__(self, values: list[tuple[Any, ...]]) -> None:
        """Hold ``values`` under :data:`FIELDS`."""
        self.schema = FIELDS
        self.total_rows = len(values)
        index = {field.name: position for position, field in enumerate(FIELDS)}
        self._rows = [bigquery.Row(row, index) for row in values]

    def _should_use_bqstorage(self, *_args: Any, **_kwargs: Any) -> bool:
        """The Storage API is never used."""
        return False

    def __iter__(self) -> Any:
        """Yield each row."""
        return iter(self._rows)


class _Client:
    """``bigquery.Client`` as far as the provider reaches it."""

    made: list["_Client"] = []
    failure: BaseException | None = None

    def __init__(self, project: str, default_query_job_config: Any = None,
                 client_options: Any = None) -> None:
        """Record the construction, or raise the failure set for it."""
        if _Client.failure is not None:
            raise _Client.failure
        self.project = project
        self.default_query_job_config = default_query_job_config
        self.client_options = client_options
        self.asked: list[tuple[str, Any]] = []
        self.query_failure: BaseException | None = None
        self.dry_run_failure: BaseException | None = None
        self.listing_failure: BaseException | None = None
        self.bound: list[dict[str, Any]] = []
        self.closed = False
        _Client.made.append(self)

    def list_datasets(self, project: str) -> list[Any]:
        """Two datasets, out of order, or raise the failure set for the listing."""
        self.asked.append(("list_datasets", project))
        if self.listing_failure is not None:
            raise self.listing_failure
        return [SimpleNamespace(dataset_id="usa_names"), SimpleNamespace(dataset_id="austin_311")]

    def list_tables(self, dataset: str) -> list[Any]:
        """One table and one view."""
        self.asked.append(("list_tables", dataset))
        return [SimpleNamespace(table_id="usa_1910_current", table_type="TABLE"),
                SimpleNamespace(table_id="names_by_state", table_type="VIEW")]

    def get_table(self, table: str) -> Any:
        """The table resource, or NotFound for any other name."""
        self.asked.append(("get_table", table))
        if not table.endswith(".usa_1910_current"):
            raise api_exceptions.NotFound("no such table")
        return SimpleNamespace(schema=FIELDS)

    def query_and_wait(self, query: str, *, job_config: Any, page_size: Any) -> _Rows:
        """Answer three rows, plan a dry run, or raise the failure set for either."""
        self.bound.append({one.name: one.value for one in job_config.query_parameters})
        if job_config.dry_run:
            self.asked.append(("dry_run", query))
            if self.dry_run_failure is not None:
                raise self.dry_run_failure
            return _Rows([])
        self.asked.append(("query_and_wait", query))
        if self.query_failure is not None:
            raise self.query_failure
        return _Rows([("TX", 1910), ("CA", 1911), ("NY", 1912)])

    def close(self) -> None:
        """Record the close."""
        self.closed = True


@pytest.fixture(autouse=True)
def client(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    """Replace the client for every test, with nothing recorded."""
    _Client.made = []
    _Client.failure = None
    monkeypatch.setattr(bigquery_utils.bigquery, "Client", _Client)
    return _Client


def _config(**settings: Any) -> SimpleNamespace:
    """A bigquery connection config, with ``settings`` replacing its fields."""
    # As the installation declares it (backlog 1407): project is the data project,
    # database the dataset, execution_project where jobs run and calls are charged.
    fields = {"name": "public", "provider": "bigquery", "project": DATA_PROJECT,
              "database": "samples", "execution_project": EXECUTION_PROJECT}
    fields.update(settings)
    return SimpleNamespace(**fields)


def _open(**settings: Any) -> Any:
    """A connection opened through DBAdapter, as every consumer opens one."""
    return DBAdapter().get_connection(_config(**settings))


class TestOpening:
    """A connection is the execution project, the data project and an optional limit."""

    def test_the_adapter_resolves_the_provider(self) -> None:
        conn = _open()

        assert isinstance(conn, bigquery_utils.BigQueryConnection)
        assert DBAdapter().provider(conn) == "bigquery"
        assert _Client.made[0].project == EXECUTION_PROJECT

    def test_the_module_prefix_resolves_without_the_attribute(self) -> None:
        conn = bigquery_utils.get_connection(_config())

        assert DBAdapter().provider(conn) == "bigquery"

    def test_calls_are_charged_to_the_execution_project_not_adcs_quota_project(self) -> None:
        # ADC's own quota project answered 403 accessNotConfigured where it had
        # the BigQuery API off; the execution project is where work is billed.
        _open()

        assert _Client.made[0].client_options.quota_project_id == EXECUTION_PROJECT

    def test_without_an_execution_project_calls_are_charged_to_the_data_project(self) -> None:
        _open(execution_project=None)

        assert _Client.made[0].project == DATA_PROJECT
        assert _Client.made[0].client_options.quota_project_id == DATA_PROJECT

    def test_no_billing_limit_unless_configured(self) -> None:
        _open()

        assert _Client.made[0].default_query_job_config is None

    def test_the_billing_limit_reaches_the_query_job_config(self) -> None:
        _open(maximum_bytes_billed=10_000_000)

        config = _Client.made[0].default_query_job_config
        assert isinstance(config, bigquery.QueryJobConfig)
        assert config.maximum_bytes_billed == 10_000_000

    @pytest.mark.parametrize("value", [0, -1, "10", 1.5, True])
    def test_a_billing_limit_must_be_a_positive_integer(self, value: Any) -> None:
        with pytest.raises(ConfigError, match="maximum_bytes_billed"):
            _open(maximum_bytes_billed=value)
        assert _Client.made == []

    @pytest.mark.parametrize("missing", ["project", "database"])
    def test_project_and_database_are_required(self, missing: str) -> None:
        with pytest.raises(ConfigError, match=r"\(project, database\)"):
            _open(**{missing: None})

    def test_an_adc_failure_is_its_class_never_its_message(self) -> None:
        _Client.failure = google.auth.exceptions.DefaultCredentialsError("/home/someone/key")

        with pytest.raises(DatabaseError) as raised:
            _open()

        assert "DefaultCredentialsError" in str(raised.value)
        assert "/home/someone" not in str(raised.value)

    def test_closing_closes_the_client_it_created(self) -> None:
        conn = _open()

        conn.close()

        assert _Client.made[0].closed


class TestCatalog:
    """Datasets first; a dataset's objects only when asked for that dataset."""

    def test_the_catalog_is_the_data_project(self) -> None:
        assert DBAdapter().list_catalogs(_open()) == [{"name": DATA_PROJECT}]

    def test_schemas_are_the_datasets_listed_alone(self) -> None:
        conn = _open()

        schemas = DBAdapter().list_schemas(conn)

        assert [one["name"] for one in schemas] == ["austin_311", "usa_names"]
        assert _Client.made[0].asked == [("list_datasets", DATA_PROJECT)]

    def test_one_datasets_tables_and_views(self) -> None:
        conn = _open()

        metadata = DBAdapter().get_schema_metadata(conn, "usa_names")

        assert [one["name"] for one in metadata["tables"]] == ["usa_1910_current"]
        assert metadata["views"] == [{"name": "names_by_state"}]
        assert _Client.made[0].asked == [("list_tables", f"{DATA_PROJECT}.usa_names")]

    def test_a_tables_columns_and_types(self) -> None:
        columns = DBAdapter().table_column_catalog(_open(), "usa_names", "usa_1910_current")

        assert columns == [
            {"name": "state", "type": "STRING", "default": None, "nullable": True},
            {"name": "year", "type": "INTEGER", "default": "1910", "nullable": False},
        ]

    def test_an_absent_table_has_no_columns_and_does_not_exist(self) -> None:
        conn = _open()

        assert DBAdapter().table_column_catalog(conn, "usa_names", "absent") == []
        assert DBAdapter().table_exists(conn, "", "usa_names", "absent") is False
        assert DBAdapter().table_exists(conn, "", "usa_names", "usa_1910_current") is True

    def test_metadata_capabilities_are_the_five_it_answers(self) -> None:
        conn = _open()
        adapter = DBAdapter()
        asked = ("catalogs", "schemas", "tables", "views", "columns", "primary_keys",
                 "foreign_keys", "indexes", "procedures", "functions")

        assert {name for name in asked if adapter.supports(conn, name)} == {
            "catalogs", "schemas", "tables", "views", "columns"}

    def test_an_api_failure_is_its_class_and_http_status(self) -> None:
        conn = _open()
        conn.client.listing_failure = api_exceptions.Forbidden("user x lacks access to y")

        with pytest.raises(DatabaseError) as raised:
            DBAdapter().list_schemas(conn)

        assert "Forbidden; HTTP 403" in str(raised.value)
        assert "lacks access" not in str(raised.value)


class TestReading:
    """Queries run through DBAdapter's existing DB-API path and are bounded."""

    def test_a_bounded_select_returns_at_most_its_limit(self) -> None:
        conn = _open()

        columns, rows = DBAdapter().query_rows(conn, QUERY, limit=2)

        assert columns == ["state", "year"]
        assert rows == [{"state": "TX", "year": 1910}, {"state": "CA", "year": 1911}]
        assert _Client.made[0].asked == [("query_and_wait", QUERY)]

    def test_fetch_rows_binds_named_parameters(self) -> None:
        conn = _open()

        rows = bigquery_utils.fetch_rows(conn, "SELECT :state AS state, 'A%' AS mask",
                                         {"state": "TX"})

        assert len(rows) == 3
        # The DB-API renders a bound name as a quoted query parameter, and the
        # doubled literal percent back as one.
        assert _Client.made[0].asked == [
            ("query_and_wait", "SELECT @`state` AS state, 'A%' AS mask")]

    def test_a_query_failure_is_its_class_and_http_status(self) -> None:
        conn = _open()
        conn.client.query_failure = api_exceptions.BadRequest("value 'secret' is invalid")

        with pytest.raises(DatabaseError) as raised:
            bigquery_utils.fetch_rows(conn, QUERY, {})

        assert "BadRequest; HTTP 400" in str(raised.value)
        assert "secret" not in str(raised.value)

    def test_identifiers_are_quoted_in_googlesql(self) -> None:
        assert DBAdapter().quote_identifier(_open(), "a`b") == "`a\\`b`"


class TestOpeningStatement:
    """A table opens on its project-qualified name, not on the execution project's."""

    def test_the_reference_is_project_qualified(self) -> None:
        reference = DBAdapter().relation_reference(
            _open(), DATA_PROJECT, "usa_names", "usa_1910_current")

        assert reference == "`public-data`.`usa_names`.`usa_1910_current`"

    def test_an_empty_catalog_is_the_connections_data_project(self) -> None:
        # Opened with no catalog, the table was read from the data project but
        # named as dataset.table -- which BigQuery looked up in the execution
        # project, answering 404.
        reference = DBAdapter().relation_reference(_open(), "", "usa_names", "usa_1910_current")

        assert reference == "`public-data`.`usa_names`.`usa_1910_current`"

    def test_a_table_read_through_its_schema_opens_on_that_reference(self) -> None:
        from rey_lib.db.database_objects import DatabaseSchema

        read = DatabaseSchema.read(_open(), "public", DATA_PROJECT, "usa_names")

        table = read.tables[0]
        assert table.initial_select.endswith(
            "FROM `public-data`.`usa_names`.`usa_1910_current`;")
        assert "state" in table.initial_select and "year" in table.initial_select
        assert read.views[0].initial_select.endswith(
            "FROM `public-data`.`usa_names`.`names_by_state`;")


class TestRefusals:
    """What the provider does not implement is refused by name, not by AttributeError."""

    @pytest.mark.parametrize("call, arguments", [
        ("execute_statement", ("statement",)),
        ("insert", ("schema", "table", {"a": 1})),
        ("update", ("schema", "table", {"a": 1}, {"b": 2})),
        ("delete", ("schema", "table", {"b": 2})),
    ])
    def test_writes_are_refused_by_name(self, call: str, arguments: tuple[Any, ...]) -> None:
        with pytest.raises(UnsupportedDatabaseCapabilityError, match=f"'{call}'"):
            getattr(DBAdapter(), call)(_open(), *arguments)

    def test_the_provider_is_read_only_by_scope(self) -> None:
        """The four write operations are deliberately absent (backlog 1365).

        BigQuery is a read-only data source here. Asserting the absence keeps
        "declared unsupported" honest, as the postgres known-gaps test does: if
        one is implemented later, this fails and says so.
        """
        for capability in ("execute_statement", "insert", "update", "delete"):
            assert not hasattr(bigquery_utils, capability), capability



class TestPaging:
    """The Workbench's page, answered as the other providers answer it."""

    def test_a_page_is_planned_then_read_with_one_row_beyond_it(self) -> None:
        conn = _open()

        page = DBAdapter().execute_page(conn, QUERY, offset=0, limit=2)

        assert page.columns == ["state", "year"]
        assert page.rows == [{"state": "TX", "year": 1910}, {"state": "CA", "year": 1911}]
        assert page.total_row_count is None
        assert (page.offset, page.limit, page.next_offset) == (0, 2, 2)
        asked = _Client.made[0].asked
        assert [kind for kind, _ in asked] == ["dry_run", "query_and_wait"]
        assert asked[0][1] == asked[1][1]
        assert asked[1][1].endswith("LIMIT 3 OFFSET 0")
        assert "(SELECT 1) AS rey_page" in asked[1][1]

    def test_the_last_page_has_no_continuation(self) -> None:
        page = DBAdapter().execute_page(_open(), QUERY, offset=4, limit=3)

        assert page.next_offset is None
        assert _Client.made[0].asked[1][1].endswith("LIMIT 4 OFFSET 4")

    def test_the_workbench_query_pages_through_the_provider(self) -> None:
        from rey_lib.db.database_query import DatabaseQuery

        page = DatabaseQuery(QUERY).page(_open(), offset=0, limit=50)

        assert len(page.rows) == 3 and page.next_offset is None

    def test_filter_values_are_bound_and_columns_quoted(self) -> None:
        conn = _open()

        DBAdapter().execute_page(conn, QUERY, offset=0, limit=10, filters=[
            {"column": "state", "operator": "equals", "value": "TX"},
            {"column": "na`me", "operator": "contains", "value": "Ann"},
        ])

        statement = _Client.made[0].asked[1][1]
        assert "`state` = @`filter_0`" in statement
        assert ("LOWER(CAST(`na\\`me` AS STRING)) LIKE CONCAT('%', LOWER(@`filter_1`), '%')"
                in statement)
        assert "TX" not in statement and "Ann" not in statement
        assert _Client.made[0].bound[-1] == {"filter_0": "TX", "filter_1": "Ann"}

    def test_a_literal_percent_in_the_readers_query_survives(self) -> None:
        DBAdapter().execute_page(_open(), "SELECT 'A%' AS mask", offset=0, limit=10)

        assert "(SELECT 'A%' AS mask) AS rey_page" in _Client.made[0].asked[1][1]

    def test_ordering_is_quoted_and_directed(self) -> None:
        DBAdapter().execute_page(_open(), QUERY, offset=0, limit=10, order_by=[
            {"column": "year", "direction": "desc"}, {"column": "state"}])

        assert "ORDER BY `year` DESC, `state` ASC" in _Client.made[0].asked[1][1]

    @pytest.mark.parametrize("bad", [
        {"offset": -1, "limit": 10}, {"offset": 0, "limit": 0}, {"offset": 0, "limit": 501},
    ])
    def test_bounds_are_refused_not_clamped(self, bad: dict[str, int]) -> None:
        with pytest.raises(DatabaseError):
            DBAdapter().execute_page(_open(), QUERY, **bad)
        assert _Client.made[0].asked == []

    @pytest.mark.parametrize("bad", [
        {"column": "state", "operator": "like", "value": "x"},
        {"column": "", "value": "x"},
        {"column": "doc", "operator": "equals", "value": "x", "path": ["a"]},
    ])
    def test_a_filter_it_cannot_write_is_refused_before_anything_runs(
        self, bad: dict[str, Any],
    ) -> None:
        with pytest.raises(DatabaseError):
            DBAdapter().execute_page(_open(), QUERY, offset=0, limit=10, filters=[bad])
        assert _Client.made[0].asked == []

    def test_a_statement_bigquery_cannot_plan_is_declined_without_running(self) -> None:
        conn = _open()
        conn.client.dry_run_failure = api_exceptions.BadRequest("near 'secret'")

        with pytest.raises(UnsupportedDatabaseCapabilityError, match="cannot be paged") as raised:
            DBAdapter().execute_page(conn, QUERY, offset=0, limit=10)

        assert "BadRequest; HTTP 400" in str(raised.value)
        assert "secret" not in str(raised.value)
        assert [kind for kind, _ in _Client.made[0].asked] == ["dry_run"]

    def test_any_other_planning_failure_is_a_failure_not_a_declination(self) -> None:
        conn = _open()
        conn.client.dry_run_failure = api_exceptions.Forbidden("no access")

        with pytest.raises(DatabaseError, match="Forbidden; HTTP 403") as raised:
            DBAdapter().execute_page(conn, QUERY, offset=0, limit=10)

        assert not isinstance(raised.value, UnsupportedDatabaseCapabilityError)

    def test_a_failed_page_read_is_its_class_and_http_status(self) -> None:
        conn = _open()
        conn.client.query_failure = api_exceptions.BadRequest("bytes billed exceeded for 'x'")

        with pytest.raises(DatabaseError, match="BadRequest; HTTP 400") as raised:
            DBAdapter().execute_page(conn, QUERY, offset=0, limit=10)

        assert not isinstance(raised.value, UnsupportedDatabaseCapabilityError)
        assert "bytes billed" not in str(raised.value)
