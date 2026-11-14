"""
BigQuery provider for DBAdapter (backlog 1365).

The fifth provider, added through the path ``db_adapter.py`` documents: this
module, one ``_REGISTRY`` entry, one ``_MODULE_PREFIXES`` entry, and
``provider: bigquery`` in the connection config. It implements the provider
functions BigQuery can answer, paging included; every other provider
capability -- the writes among them -- is absent, and DBAdapter refuses it by name.

Connection config
-----------------
``provider: bigquery``
``project``                 The execution project: the GCP project query jobs
                            run and are billed in. Required.
``database``                The data project, read as the catalog -- BigQuery's
                            project, dataset and table are Rey's catalog,
                            schema and table. Required.
``maximum_bytes_billed``    Optional. Passed to BigQuery's query job
                            configuration; a query that would bill more fails.

Authentication is Application Default Credentials. No credential is read from
the connection config.

What may run is the authenticated identity's IAM answer. Nothing here reads or
classifies SQL text.

Public API
----------
get_connection(db_cfg, ctx=)                       Open a connection through ADC.
get_current_database(conn)                         The data project.
list_database_objects(conn, database=)             The data project's datasets.
schema_metadata(conn, schema, database_name=)      One dataset's tables and views.
table_column_catalog(conn, schema, table, ...)     One table's or view's columns.
table_exists(conn, catalog, schema, table)         Whether a table or view exists.
quote_identifier(name)                             A GoogleSQL quoted identifier.
relation_reference(catalog, schema, name)          A project-qualified table reference.
fetch_rows(conn, sql_text, params)                 One read with ``:name`` parameters.
execute_page(conn, sql_text, offset=, limit=, ...) One page of a reader's query.
"""

from __future__ import annotations

import re
from typing import Any

import google.auth.exceptions
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery
from google.cloud.bigquery import dbapi

from rey_lib.errors.error_utils import AppError, ConfigError, DatabaseError
from rey_lib.logs import AppLogger

__all__ = [
    "BigQueryConnection",
    "get_connection",
    "get_current_database",
    "list_database_objects",
    "schema_metadata",
    "table_column_catalog",
    "table_exists",
    "quote_identifier",
    "relation_reference",
    "fetch_rows",
    "execute_page",
]

_logger = AppLogger(__name__)

#: BigQuery's table types that are views; every other listed type is a table.
_VIEW_TYPES = frozenset({"VIEW", "MATERIALIZED_VIEW"})

#: The mode BigQuery gives a column that cannot be NULL.
_REQUIRED_MODE = "REQUIRED"

#: A named ``:name`` parameter -- never a ``::`` cast or part of a word.
_NAMED_PARAMETER = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")

#: The failures Google's libraries raise: an API call, and credential resolution.
_GOOGLE_ERRORS = (api_exceptions.GoogleAPIError, google.auth.exceptions.GoogleAuthError)


def _reason(exc: BaseException) -> str:
    """A Google failure as its class and HTTP status, never its message (#1357).

    The message can quote the query or a value that failed, so it is not kept.
    """
    if isinstance(exc, AppError):
        # Already rey_lib's own governed message, not the driver's.
        return str(exc)
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}; HTTP {code}" if isinstance(code, int) else type(exc).__name__


class BigQueryConnection(dbapi.Connection):
    """BigQuery's DB-API connection, carrying the client and data project it was opened with.

    DBAdapter's DB-API paths execute through the inherited cursor; the catalog
    reads here use the client's API directly, so they run no query job.
    """

    def __init__(self, client: bigquery.Client, database: str) -> None:
        """Open over ``client``; ``database`` is the data project read as the catalog.

        Results are read through the client's REST API: the BigQuery Storage
        client is not a declared dependency, so it is not asked for.
        """
        super().__init__(client=client, prefer_bqstorage_client=False)
        self.client = client
        self.database = database

    def close(self) -> None:
        """Close the connection and the client it was opened with, which this provider created."""
        super().close()
        self.client.close()


def _setting(db_cfg: Any, name: str) -> Any:
    """Read one field from a connection config, mapping or namespace alike."""
    if isinstance(db_cfg, dict):
        return db_cfg.get(name)
    return getattr(db_cfg, name, None)


def get_connection(db_cfg: Any, *, ctx: Any = None) -> BigQueryConnection:
    """
    Open a BigQuery connection through Application Default Credentials.

    Parameters
    ----------
    db_cfg : Any
        Connection config. Required fields: project, database. Optional:
        maximum_bytes_billed.
    ctx : Any
        Accepted so every backend answers the adapter's call the same way.
        ADC is resolved by Google's library, so nothing is read from it.

    Returns
    -------
    BigQueryConnection
        Open connection. Caller is responsible for closing it.

    Raises
    ------
    ConfigError
        If project or database is missing, or maximum_bytes_billed is not a
        positive integer.
    DatabaseError
        If ADC cannot be resolved or the client cannot be created.
    """
    name = _setting(db_cfg, "name") or "<unnamed>"
    project = _setting(db_cfg, "project")
    database = _setting(db_cfg, "database")
    if not project or not database:
        raise ConfigError(
            f"bigquery_utils: connection '{name}' is missing required fields "
            "(project, database)."
        )
    maximum = _setting(db_cfg, "maximum_bytes_billed")
    if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int)
                                or maximum <= 0):
        raise ConfigError(
            f"bigquery_utils: connection '{name}' sets maximum_bytes_billed to "
            f"{maximum!r}; it must be a positive integer."
        )
    job_config = (
        bigquery.QueryJobConfig(maximum_bytes_billed=maximum) if maximum is not None else None
    )
    _logger.debug(
        "attempting bigquery client through ADC connection=%s project=%s",
        name, project,
    )
    try:
        client = bigquery.Client(project=str(project), default_query_job_config=job_config)
    except _GOOGLE_ERRORS as exc:
        raise DatabaseError(
            f"bigquery_utils: failed to open connection '{name}': {_reason(exc)}"
        ) from None
    return BigQueryConnection(client, str(database))


def get_current_database(conn: BigQueryConnection) -> str:
    """Return the data project, which is this connection's catalog."""
    return conn.database


def list_database_objects(
    conn: BigQueryConnection, database: str | None = None,
) -> list[dict[str, Any]]:
    """Return the data project's datasets, as schemas.

    Datasets only. A dataset's tables and views are read for that dataset alone,
    by :func:`schema_metadata`; a public project holds hundreds of datasets, and
    listing every one's objects would be one API call per dataset.
    """
    project = database or get_current_database(conn)
    _logger.debug("attempting bigquery dataset listing project=%s", project)
    try:
        datasets = list(conn.client.list_datasets(project=project))
    except _GOOGLE_ERRORS as exc:
        raise DatabaseError(f"bigquery_utils: dataset listing failed: {_reason(exc)}") from None
    return [
        {
            "database": project,
            "object_type": "schema",
            "schema": str(dataset.dataset_id),
            "name": str(dataset.dataset_id),
            "dependencies": [],
        }
        for dataset in sorted(datasets, key=lambda one: str(one.dataset_id))
    ]


def schema_metadata(
    conn: BigQueryConnection, schema: str, *, database_name: str = "",
) -> dict[str, Any]:
    """One dataset's tables and views, in the shape every provider answers (backlog 725).

    ``database_name`` names the project the dataset is in; empty for the data
    project. BigQuery declares no keys, indexes or constraints Rey reads, so
    those lists are empty.
    """
    project = database_name or get_current_database(conn)
    _logger.debug("attempting bigquery table listing project=%s dataset=%s", project, schema)
    try:
        listed = list(conn.client.list_tables(f"{project}.{schema}"))
    except _GOOGLE_ERRORS as exc:
        raise DatabaseError(f"bigquery_utils: table listing failed: {_reason(exc)}") from None
    tables: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for item in listed:
        name = str(item.table_id)
        if str(item.table_type or "").upper() in _VIEW_TYPES:
            views.append({"name": name})
        else:
            tables.append({"name": name, "primary_key": {}, "foreign_keys": [], "indexes": [],
                           "unique_constraints": [], "check_constraints": []})
    return {"tables": tables, "views": views}


def _table(conn: BigQueryConnection, project: str, schema: str, table: str) -> Any:
    """The client's table resource, or None when BigQuery has no such table or view."""
    _logger.debug(
        "attempting bigquery table read project=%s dataset=%s table=%s", project, schema, table,
    )
    try:
        return conn.client.get_table(f"{project}.{schema}.{table}")
    except api_exceptions.NotFound:
        return None
    except _GOOGLE_ERRORS as exc:
        raise DatabaseError(f"bigquery_utils: table read failed: {_reason(exc)}") from None


def table_column_catalog(
    conn: BigQueryConnection, schema: str, table: str, *, database_name: str = "",
) -> list[dict[str, Any]]:
    """One table's or view's columns as BigQuery's schema states them, in column order.

    A metadata primitive (backlog 724): the type is BigQuery's field type, a
    column is nullable unless its mode is REQUIRED, and the default is the
    column's default value expression. Empty when there is no such table.

    Returns:
        ``{"name", "type", "default", "nullable"}`` per column.
    """
    found = _table(conn, database_name or get_current_database(conn), schema, table)
    if found is None:
        return []
    return [
        {
            "name": str(field.name),
            "type": str(field.field_type),
            "default": field.default_value_expression,
            "nullable": str(field.mode or "").upper() != _REQUIRED_MODE,
        }
        for field in found.schema
    ]


def table_exists(conn: BigQueryConnection, catalog: str, schema: str, table: str) -> bool:
    """Whether BigQuery holds a table or view of this dataset and name.

    A metadata primitive (backlog 698). ``catalog`` is the project; empty for
    the data project.
    """
    return _table(conn, catalog or get_current_database(conn), schema, table) is not None


def quote_identifier(name: str) -> str:
    """Quote one identifier in GoogleSQL: backticks, with backslash and backtick escaped."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def relation_reference(catalog: str, schema: str, name: str) -> str:
    """One table or view as a GoogleSQL statement names it: project, dataset and name.

    Always project-qualified: BigQuery resolves an unqualified dataset.table
    against the execution project, which is not where the data project's
    tables are.
    """
    return ".".join(quote_identifier(part) for part in (catalog, schema, name) if part)


def fetch_rows(conn: BigQueryConnection, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one read query with named ``:name`` parameters and return its rows.

    A connector primitive: the statement is the caller's, and binding is this
    driver's -- the DB-API's pyformat names a parameter ``%(name)s``, so a
    literal ``%`` is doubled and each ``:name`` is written that way.

    Raises:
        DatabaseError: If the query fails.
    """
    statement = _NAMED_PARAMETER.sub(
        lambda match: "%(" + match.group(1) + ")s", _pyformat(sql_text),
    )
    _logger.debug("attempting bigquery read project=%s", conn.client.project)
    try:
        columns, rows = _run(conn, statement, dict(params))
    except dbapi.Error as exc:
        raise DatabaseError(
            f"bigquery_utils: fetch_rows failed: {_reason(_cause(exc))}"
        ) from None
    return [dict(zip(columns, row)) for row in rows]


def _pyformat(sql_text: str) -> str:
    """SQL text written for the DB-API's pyformat: each literal ``%`` doubled.

    The DB-API turns ``%%`` back into ``%`` whether or not a statement binds
    anything, so this is right for every statement it runs.
    """
    return str(sql_text).replace("%", "%%")


def _run(
    conn: BigQueryConnection, statement: str, params: dict[str, Any],
    job_config: Any = None,
) -> tuple[list[str], list[Any]]:
    """Run one pyformat statement on the connection's cursor; its columns and rows.

    The cursor waits for the query job to complete. A dry run plans the
    statement and has no rows, so none are fetched. A failure is left as the
    DB-API raised it, for the caller to say what was being attempted.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(statement, params or None, job_config=job_config)
        if job_config is not None and job_config.dry_run:
            return [], []
        columns = [str(column[0]) for column in cursor.description or []]
        return columns, (list(cursor.fetchall()) if columns else [])
    finally:
        cursor.close()


def _cause(exc: BaseException) -> BaseException:
    """The Google failure a DB-API error wraps, or the error itself."""
    wrapped = exc.args[0] if exc.args else None
    return wrapped if isinstance(wrapped, BaseException) else exc


# ---------------------------------------------------------------------------
# Paging
#
# One page of a reader's own query, as the other providers answer it. Every
# dialect fact lives here; DBAdapter only dispatches. Like DuckDB's, a page
# states no total: counting would be a second billed query job on every page,
# so continuation is answered by reading one row past the page.
# ---------------------------------------------------------------------------

#: The largest page a caller may ask for. The rule is the estate's
#: (rey_lib.logs.file_hierarchy), which every provider follows: a page size
#: taken on trust from a caller is an unbounded read wearing a parameter.
_MAX_PAGE_ROWS = 500

#: What the wrapped query is called, so a filter or an order naming a column is
#: unambiguous.
_PAGE_ALIAS = "rey_page"

#: Filter operators, by the name a caller uses. The operator and the identifier
#: are written; every value is bound. Written for pyformat, so a literal percent
#: is doubled. GoogleSQL has no ILIKE, so a text match compares lowercase text.
_PAGE_FILTER_OPERATORS: dict[str, str] = {
    "equals": "{column} = {value}",
    "notEquals": "{column} <> {value}",
    "contains": "LOWER(CAST({column} AS STRING)) LIKE CONCAT('%%', LOWER({value}), '%%')",
    "notContains": "LOWER(CAST({column} AS STRING)) NOT LIKE CONCAT('%%', LOWER({value}), '%%')",
    "startsWith": "LOWER(CAST({column} AS STRING)) LIKE CONCAT(LOWER({value}), '%%')",
    "endsWith": "LOWER(CAST({column} AS STRING)) LIKE CONCAT('%%', LOWER({value}))",
    "gt": "{column} > {value}",
    "gte": "{column} >= {value}",
    "lt": "{column} < {value}",
    "lte": "{column} <= {value}",
}


def _validated_page_bounds(offset: int, limit: int) -> tuple[int, int]:
    """Return the page bounds, refused rather than clamped when out of range."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DatabaseError("offset must be a nonnegative integer.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DatabaseError("limit must be a positive integer.")
    if limit > _MAX_PAGE_ROWS:
        raise DatabaseError(f"limit must not exceed {_MAX_PAGE_ROWS}.")
    return offset, limit


def _page_wrapped(sql_text: str) -> str:
    """Return the reader's query as a pyformat subquery this module can build on."""
    inner = str(sql_text or "").strip().rstrip(";").strip()
    if not inner:
        raise DatabaseError("SQL text is required.")
    return f"({_pyformat(inner)}) AS {_PAGE_ALIAS}"


def _page_filters(
    filters: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any]]:
    """Return the WHERE clause and the values it binds, by parameter name.

    A filter on a nested value (``path``, #1143) is refused: GoogleSQL's JSON
    functions take their path as a literal, and a path is never written into a
    statement, only bound.
    """
    if not filters:
        return "", {}
    clauses: list[str] = []
    values: dict[str, Any] = {}
    for position, one in enumerate(filters):
        column = str(one.get("column") or "").strip()
        operator = str(one.get("operator") or "equals").strip()
        if not column:
            raise DatabaseError("A filter must name a column.")
        template = _PAGE_FILTER_OPERATORS.get(operator)
        if template is None:
            raise DatabaseError(
                f"Unknown filter operator '{operator}'. "
                f"Known operators: {sorted(_PAGE_FILTER_OPERATORS)}."
            )
        if one.get("path") is not None:
            raise DatabaseError("This provider cannot filter on a value nested in a column.")
        name = f"filter_{position}"
        clauses.append(template.format(
            column=quote_identifier(column), value=f"%({name})s"))
        values[name] = one.get("value")
    return "\nWHERE  " + "\n   AND ".join(clauses), values


def _page_order(order_by: list[dict[str, Any]] | None) -> str:
    """Return the ORDER BY clause, or nothing when no order was named.

    No order is left alone rather than invented: imposing one would discard an
    ORDER BY the reader's own query already carried.
    """
    if not order_by:
        return ""
    terms: list[str] = []
    for one in order_by:
        column = str(one.get("column") or "").strip()
        if not column:
            raise DatabaseError("An ordering must name a column.")
        descending = str(one.get("direction") or "asc").lower() == "desc"
        terms.append(f"{quote_identifier(column)} {'DESC' if descending else 'ASC'}")
    return "\nORDER BY " + ", ".join(terms)


def _refuse_unpageable(
    conn: BigQueryConnection, statement: str, values: dict[str, Any],
) -> None:
    """Decline here, before the reader's statement has been executed.

    BigQuery decides, not a keyword scan: a dry run plans the paged statement
    and executes none of it, and bills nothing. A statement that cannot be read
    as a subquery -- a script, a definition, a call -- is refused by BigQuery as
    a bad request and declined without having run. Any other failure is not a
    declination and is reported as the failure it is.
    """
    from rey_lib.errors.error_utils import UnsupportedDatabaseCapabilityError

    _logger.debug("attempting bigquery dry run project=%s", conn.client.project)
    try:
        _run(conn, statement, values, job_config=bigquery.QueryJobConfig(dry_run=True))
    except dbapi.Error as exc:
        cause = _cause(exc)
        if isinstance(cause, api_exceptions.BadRequest):
            raise UnsupportedDatabaseCapabilityError(
                "This statement cannot be paged: it is not a query that can be "
                f"read as a subquery. {_reason(cause)}"
            ) from None
        raise DatabaseError(f"The page could not be planned: {_reason(cause)}") from None


def execute_page(
    conn: BigQueryConnection,
    sql_text: str,
    *,
    offset: int,
    limit: int,
    order_by: list[dict[str, Any]] | None = None,
    filters: list[dict[str, Any]] | None = None,
) -> Any:
    """Return one page of a query. **No total: this provider does not count.**

    A count would be a second query job, billed and run on every page. So
    ``total_row_count`` is None -- unknown, never zero -- and continuation is
    answered by reading one row past the page. If that row is there, another
    page follows.

    **This bounds what is returned, not what is read.** ``LIMIT`` caps result
    rows; how much BigQuery scans to produce them is its own affair, bounded
    only by the connection's ``maximum_bytes_billed`` where one is configured.

    Args:
        conn: Open BigQuery connection.
        sql_text: The query to page, exactly as the reader wrote it.
        offset: Where the page starts.
        limit: The most rows the page may hold.
        order_by: Column/direction mappings, or None to leave the query's own
            ordering alone.
        filters: Column/operator/value mappings, or None.

    Returns:
        One ``PageResult`` whose ``total_row_count`` is None and whose
        ``next_offset`` is set when a further page exists.

    Raises:
        UnsupportedDatabaseCapabilityError: If this statement cannot be paged.
            Raised before the statement is executed, never after.
        DatabaseError: If the bounds or filters are invalid, or execution failed.
    """
    from rey_lib.db.database_query import PageResult

    offset, limit = _validated_page_bounds(offset, limit)
    wrapped = _page_wrapped(sql_text)
    where, values = _page_filters(filters)
    order = _page_order(order_by)
    # offset and limit are written in rather than bound: they are integers this
    # module validated. One row beyond the page is the continuation signal.
    statement = (
        f"SELECT * FROM {wrapped}{where}{order}"
        f"\nLIMIT {limit + 1:d} OFFSET {offset:d}"
    )

    # Declined here or not at all. Nothing below this line may raise
    # UnsupportedDatabaseCapabilityError.
    _refuse_unpageable(conn, statement, values)
    _logger.debug(
        "attempting bigquery page project=%s offset=%s limit=%s",
        conn.client.project, offset, limit,
    )
    try:
        columns, fetched = _run(conn, statement, values)
    except dbapi.Error as exc:
        raise DatabaseError(f"The page could not be read: {_reason(_cause(exc))}") from None

    has_more = len(fetched) > limit
    return PageResult(
        columns=columns,
        rows=[dict(zip(columns, row)) for row in fetched[:limit]],
        # Not counted: a count is a second billed query job on every page. None
        # is unknown, and the surfaces above say so.
        total_row_count=None,
        offset=offset,
        limit=limit,
        next_offset=offset + limit if has_more else None,
    )
