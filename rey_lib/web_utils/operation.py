"""One governed HTTP job, executed generically (backlog 813).

    control.http_operation_vw -> f_http_operation_get -> Control.http_operation
        -> HttpOperation.hydrate(job) -> execute(records, ctx)
        -> HttpConnection built from the job -> HttpRequest -> remote API

**The job is the database's.** It arrives as one flat, typed row: the
operation, the connection it runs over, its request fields, value maps and
response fields as paired arrays, and its scalar settings. Nothing here reads a
configuration table, resolves a connection by name or interprets a
configuration language. No provider is named in this module -- a site is rows.

**This object executes the job** -- it builds the job's ``HttpConnection``,
fills each request from the input records, batches, retries, follows link
pagination, correlates answers to inputs and reads the configured response
fields. The connection owns transport and credentials; the secret reference is
handed to it and resolved there, never here.

**Two kinds of failure stay apart.** An HTTP status outside 2xx that survives
the retries is an execution failure and raises; so does a position-correlated
answer whose length is not the batch's. A provider's own answer that an input
is in error, or has no match, is data: a row whose ``operation_status`` says
so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional

import jmespath

from rey_lib.errors.error_utils import ConfigError, HttpTransportError, StateError
from rey_lib.files.csv import read_csv_text
from rey_lib.logs import AppLogger

__all__ = ["HttpOperation", "OPERATION_STATUS", "OPERATION_MESSAGE"]

_logger = AppLogger(__name__)

#: The two columns every output row adds after its record's own and the fields.
OPERATION_STATUS = "operation_status"
OPERATION_MESSAGE = "operation_message"

#: A field whose input column is another field's selection, through its value map.
_SELECTED_REFERENCE = "selected_reference"
#: The one field kind whose value is sent as written -- and the only one a reader sets.
_LITERAL = "literal"

#: How waits are taken; a test replaces it rather than sleeping.
_sleep = time.sleep


@dataclass(frozen=True)
class _RequestField:
    """One request field of the job: a reference to an input column, or a literal.

    ``label`` is what a reader is shown for it, as governed; None where none
    is. ``name`` stays the name sent.
    """

    part: str
    name: str
    kind: str
    value: str
    label: Optional[str] = None


@dataclass(frozen=True)
class HttpOperation:
    """One complete HTTP job as its configuration row states it (backlog 813)."""

    adapter_key: str
    connection_name: str
    base_url: str
    auth_header: Optional[str]
    auth_prefix: Optional[str]
    auth_secret_ref: Optional[str]
    auth_required: bool
    timeout_seconds: int
    verify: Any
    default_headers: Optional[Mapping[str, Any]]
    http_method: str
    path: str
    request_format: str
    response_format: str
    request_fields: tuple[_RequestField, ...]
    value_maps: Mapping[tuple[str, str], Mapping[str, str]]
    response_fields: tuple[tuple[str, str], ...]
    batch_size: Optional[int]
    retry_max_attempts: Optional[int]
    retry_status_codes: tuple[int, ...]
    correlation_mode: Optional[str]
    retry_backoff: Optional[str]
    pagination_mode: Optional[str]
    pagination_next_path: Optional[str]
    response_records_path: Optional[str]
    response_error_path: Optional[str]
    response_message_path: Optional[str]
    response_text_format: Optional[str]
    #: What a reader is shown for this job, as governed; None where none is.
    display_name: Optional[str] = None

    # -- hydration ------------------------------------------------------------

    @classmethod
    def hydrate(cls, job: Mapping[str, Any]) -> "HttpOperation":
        """The operation one job row states, refused when it cannot be executed.

        Takes the row as the view shaped it. Kinds, allowed values and
        requiredness were governed when the configuration was written; this
        refuses only what the job itself makes unexecutable.

        Raises:
            ConfigError: Naming the job and the rule it breaks.
        """
        key = str(job.get("adapter_key") or "")
        if not job.get("is_enabled"):
            _refuse(key, "it is disabled")
        fields = tuple(
            _RequestField(part, name, kind, value, label)
            for part in ("body", "query", "header")
            for name, kind, value, label in zip(
                job.get(f"request_{part}_fields") or (),
                job.get(f"request_{part}_kinds") or (),
                job.get(f"request_{part}_values") or (),
                # PAIRED BY POSITION like the three above; a row without the
                # array governs no label for any of its fields.
                job.get(f"request_{part}_labels")
                or (None,) * len(job.get(f"request_{part}_fields") or ()),
            )
        )
        value_maps: dict[tuple[str, str], dict[str, str]] = {}
        for part, name, given, sent in zip(
            job.get("value_map_parts") or (), job.get("value_map_fields") or (),
            job.get("value_map_inputs") or (), job.get("value_map_outputs") or (),
        ):
            value_maps.setdefault((part, name), {})[given] = sent
        operation = cls(
            adapter_key=key,
            connection_name=str(job.get("connection_name") or ""),
            base_url=str(job.get("base_url") or ""),
            auth_header=job.get("auth_header"),
            auth_prefix=job.get("auth_prefix"),
            auth_secret_ref=job.get("auth_secret_ref"),
            auth_required=bool(job.get("auth_required")),
            timeout_seconds=job.get("timeout_seconds"),
            verify=job.get("verify"),
            default_headers=job.get("default_headers"),
            http_method=str(job.get("http_method") or ""),
            path=str(job.get("path") or ""),
            request_format=str(job.get("request_format") or ""),
            response_format=str(job.get("response_format") or ""),
            request_fields=fields,
            value_maps=value_maps,
            response_fields=tuple(zip(
                job.get("response_field_names") or (), job.get("response_field_paths") or (),
            )),
            batch_size=job.get("batch_size"),
            retry_max_attempts=job.get("retry_max_attempts"),
            retry_status_codes=tuple(job.get("retry_status_codes") or ()),
            correlation_mode=job.get("correlation_mode"),
            retry_backoff=job.get("retry_backoff"),
            pagination_mode=job.get("pagination_mode"),
            pagination_next_path=job.get("pagination_next_path"),
            response_records_path=job.get("response_records_path"),
            response_error_path=job.get("response_error_path"),
            response_message_path=job.get("response_message_path"),
            response_text_format=job.get("response_text_format"),
            display_name=job.get("display_name"),
        )
        operation._check_job()
        return operation

    def _check_job(self) -> None:
        """Refuse a job whose own columns make it unexecutable."""
        key = self.adapter_key
        position = self.correlation_mode == "position"
        if self.correlation_mode is None:
            _refuse(key, "correlation_mode is not set")
        if position and self.batch_size is None:
            _refuse(key, "position correlation needs batch_size")
        if self.correlation_mode == "request" and self.batch_size not in (None, 1):
            _refuse(key, "request correlation is one input per HTTP request, so batch_size must be 1")
        if self.pagination_mode == "link" and not self.pagination_next_path:
            _refuse(key, "link pagination needs pagination_next_path")
        if position and self.pagination_mode == "link":
            _refuse(key, "link pagination under position correlation is not supported")
        if self.response_text_format and self.response_format != "text":
            _refuse(key, "response_text_format applies only to a text response")
        if any(f.part == "body" for f in self.request_fields) and self.request_format != "json":
            _refuse(key, "body fields are sent only as a json request")
        if position and (self.batch_size or 0) > 1 and any(
            f.part in ("query", "header") and f.kind in ("reference", _SELECTED_REFERENCE)
            for f in self.request_fields
        ):
            _refuse(key, "a query or header field referencing an input is not supported under "
                         "position correlation with batch_size above 1")
        literals = {f.name: f for f in self.request_fields if f.kind == _LITERAL}
        for field in self.request_fields:
            if field.kind != _SELECTED_REFERENCE:
                continue
            selector = literals.get(field.value)
            if selector is None:
                _refuse(key, f"{field.part} field {field.name} selects through {field.value}, "
                             "which is not a literal field of the request")
            # EVERY SELECTION NAMES A COLUMN: each value the selector can hold
            # -- its own and its map's inputs -- has an entry in this field's map.
            columns = self.value_maps.get((field.part, field.name)) or {}
            selectable = {selector.value, *(self.value_maps.get((selector.part, selector.name)) or {})}
            uncovered = sorted(one for one in selectable if one not in columns)
            if uncovered:
                _refuse(key, f"{field.part} field {field.name} names no column for "
                             f"{field.value} {', '.join(uncovered)}")
        known = {(f.part, f.name) for f in self.request_fields}
        for part, name in self.value_maps:
            if (part, name) not in known:
                _refuse(key, f"a value map names {part} field {name}, which the request does not have")

    # -- what it reads and produces -----------------------------------------

    def check_references(self, columns: Any) -> None:
        """Refuse input that cannot run through this job, before anything is sent.

        The one pre-execution check: no response field would overwrite an
        input column. A referenced column the input does not supply is not
        refused -- it is the same as a value not populated, and the field is
        left out of each request (load.http_request_selected_reference).
        :meth:`execute` makes it; a caller that knows its columns earlier makes
        the same call.

        Raises:
            ConfigError: Naming the columns, never a value.
        """
        supplied = set(columns)
        colliding = sorted(name for name, _ in self.response_fields if name in supplied)
        if colliding:
            _refuse(self.adapter_key, f"response field(s) {', '.join(colliding)} would overwrite "
                                      "an input column of the same name")

    def output_columns(self, input_columns: list[str]) -> list[str]:
        """The input columns, the response fields, then status and message. Sends nothing."""
        return [*input_columns, *(name for name, _ in self.response_fields),
                OPERATION_STATUS, OPERATION_MESSAGE]

    # -- execution ------------------------------------------------------------

    def execute(self, records: list[dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
        """Send the records through the job and return what came back.

        Sent through the job's connection as the shared owner holds it
        (backlog 838): ``shared_connection`` with the job's governed definition,
        built once and closed at runtime collection, never here. ``ctx`` is what
        that connection resolves the secret reference through.

        Raises:
            ConfigError: When the input cannot run through this job, or the job
                reaches something this executor does not implement.
            HttpTransportError: When a status outside 2xx survives the retries,
                or the connection could not send.
            StateError: When a position-correlated answer's length is not the
                batch's.
        """
        # rey_lib.db reaches web_utils, not the other way.
        from rey_lib.db.connection import shared_connection  # noqa: PLC0415

        records = [dict(record) for record in records]
        if not records:
            return []
        self.check_references({column for record in records for column in record})
        connection = shared_connection(
            ctx, str(self.connection_name), definition=self._connection_config(),
        )
        size = int(self.batch_size) if self.correlation_mode == "position" else 1
        produced: list[dict[str, Any]] = []
        batches = [records[start:start + size] for start in range(0, len(records), size)]
        for number, batch in enumerate(batches, start=1):
            _logger.debug(
                "http operation %s: batch %d of %d, %d input(s), connection %s",
                self.adapter_key, number, len(batches), len(batch), self.connection_name,
            )
            produced.extend(self._batch(batch, connection))
        return produced

    def _connection_config(self) -> dict[str, Any]:
        """The job's connection definition, as HttpConnection takes it.

        The secret stays a reference.

        Its identity only: a job's ``default_headers`` are its own request
        semantics and travel on each request (backlog 838), so jobs carrying
        different headers can share one connection.
        """
        config: dict[str, Any] = {
            "name": self.connection_name,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "verify": self.verify,
        }
        if self.auth_header:
            auth: dict[str, Any] = {
                "header": self.auth_header,
                "value": self.auth_secret_ref,
                "required": self.auth_required,
            }
            if self.auth_prefix is not None:
                auth["prefix"] = self.auth_prefix
            config["auth"] = auth
        return config

    def _batch(self, batch: list[dict[str, Any]], connection: Any) -> list[dict[str, Any]]:
        """The output rows of one batch: refused records first, then the answered."""
        rows: list[dict[str, Any]] = []
        sent: list[tuple[dict[str, Any], dict[str, dict[str, Any]]]] = []
        for record in batch:
            filled, refusal = self._filled(record)
            if refusal is not None:
                rows.append(self._row(record, None, "error", refusal))
            else:
                sent.append((record, filled))
        if not sent:
            return rows
        answers = self._answers(sent, connection)
        for (record, _), pages in zip(sent, answers):
            rows.extend(self._rows(record, pages))
        return rows

    def _filled(self, record: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
        """The record's request parts, or why the record cannot be sent.

        A reference whose column is absent or whose value is null leaves its
        field out. A selected_reference reads the column its selection names
        and refuses the record where that column holds nothing. A value-mapped
        literal or reference must be a string with an entry in its map.
        """
        filled: dict[str, dict[str, Any]] = {"body": {}, "query": {}, "header": {}}
        for field in self.request_fields:
            if field.kind == _SELECTED_REFERENCE:
                # THE COLUMN IS THE SELECTION'S: the named field's selected
                # value, through this field's own value map. The value read is
                # sent as it is; a record without one is not sent.
                column = self.selected_column(field)
                value = record.get(column) if column is not None else None
                if value is None:
                    return filled, f"{field.part} field {field.name} has no value in column {column}"
                filled[field.part][field.name] = value
                continue
            value = record.get(field.value) if field.kind == "reference" else field.value
            if value is None:
                continue
            translation = self.value_maps.get((field.part, field.name))
            if translation is not None:
                if not isinstance(value, str):
                    return filled, f"the value of {field.part} field {field.name} is not a string"
                if value not in translation:
                    return filled, f"{field.part} field {field.name} has no value map entry for its value"
                value = translation[value]
            filled[field.part][field.name] = value
        return filled, None

    def selected_column(self, field: _RequestField) -> Optional[str]:
        """The input column a selected_reference field reads, or None.

        The field its ``value`` names holds the selection; this field's value
        map turns that selection into the column.
        """
        selector = next((one for one in self.request_fields if one.name == field.value), None)
        if selector is None:
            return None
        return (self.value_maps.get((field.part, field.name)) or {}).get(selector.value)

    # -- what a reader can set ------------------------------------------------

    @staticmethod
    def takes_value(field: _RequestField) -> bool:
        """Whether a value can be set for the field: only a literal's.

        A reference's column is governed, and a selected reference's is
        another field's selection (load.http_request_selected_reference).
        """
        return field.kind == _LITERAL

    def choices(self, field: _RequestField) -> list[str]:
        """The values the field can be set to: a literal's value-map inputs, in order; else none."""
        if not self.takes_value(field):
            return []
        return list(self.value_maps.get((field.part, field.name)) or ())

    def shown_value(self, field: _RequestField) -> Any:
        """What the field is, for display: a selected reference's column, else its value.

        The relationship applied to every record, never one record's value.
        """
        if field.kind == _SELECTED_REFERENCE:
            return self.selected_column(field)
        return field.value

    def check_request_value(self, part: str, name: str, value: Any) -> None:
        """Refuse a value this operation's request setting cannot take.

        The one validation, made both when a value is chosen and when the
        effective operation is built: the setting exists, and the value is one
        of its choices -- only a literal has any.

        Raises:
            ConfigError: Naming the setting and its choices.
        """
        field = next((
            one for one in self.request_fields if (one.part, one.name) == (str(part), str(name))
        ), None)
        if field is None:
            _logger.debug("http operation %s: refused -- %s field %s is not a request setting",
                          self.adapter_key, part, name)
            raise ConfigError(
                f"HTTP operation '{self.adapter_key}' has no {part} setting '{name}'."
            )
        allowed = self.choices(field)
        if str(value) not in allowed:
            _logger.debug("http operation %s: refused -- a value for %s field %s is not a choice",
                          self.adapter_key, part, name)
            raise ConfigError(
                f"HTTP operation '{self.adapter_key}': '{value}' is not a choice for {part} "
                f"setting '{name}'. Choices: {', '.join(allowed) or 'none'}."
            )

    def with_request_values(self, values: Mapping[str, Any]) -> "HttpOperation":
        """This operation with the values declared for its request settings applied.

        Every declared value is checked as :meth:`check_request_value` checks
        a choice, so a value held for a setting that takes none is refused,
        never applied. This operation is not modified.

        Args:
            values: ``{part: {name: value}}``, as a Transform's ``http-request``
                holds them.

        Raises:
            ConfigError: Naming the first value refused.
        """
        if not values:
            return self
        for part, named in values.items():
            for name, value in (named or {}).items():
                self.check_request_value(part, name, value)
        return replace(self, request_fields=tuple(
            replace(field, value=values[field.part][field.name])
            if field.name in (values.get(field.part) or {}) else field
            for field in self.request_fields
        ))

    def _answers(self, sent: list[tuple[dict[str, Any], dict[str, dict[str, Any]]]],
                 connection: Any) -> list[list[Any]]:
        """The answer pages of each sent record, in its order."""
        from rey_lib.web_utils import HttpRequest  # noqa: PLC0415

        first = sent[0][1]
        body: Any = None
        if self.request_format == "json":
            if any(f.part == "body" for f in self.request_fields):
                body = ([filled["body"] for _, filled in sent]
                        if self.correlation_mode == "position" else first["body"])
        elif self.request_format != "query":
            _refuse(self.adapter_key, f"request_format '{self.request_format}' is not implemented")
        request = HttpRequest(
            self.http_method,
            connection.format_url(self.path),
            params=first["query"] or None,
            headers=self._headers(first["header"]),
            json=body if self.request_format == "json" else None,
        )
        payload = self._parsed(self._sent(connection, request))
        if self.correlation_mode == "position":
            if not isinstance(payload, list) or len(payload) != len(sent):
                _logger.debug("http operation %s: refused -- a position answer does not match "
                              "its batch of %d", self.adapter_key, len(sent))
                raise StateError(
                    f"http operation {self.adapter_key}: a position-correlated answer must be one "
                    f"element per input; {len(sent)} sent, "
                    f"{len(payload) if isinstance(payload, list) else 'not a list'} answered."
                )
            return [[element] for element in payload]
        if self.correlation_mode != "request":
            _refuse(self.adapter_key, f"correlation_mode '{self.correlation_mode}' is not implemented")
        pages = [payload]
        while self.pagination_mode == "link":
            following = _search(self.pagination_next_path, pages[-1])
            if not following:
                break
            _logger.debug("http operation %s: following the next page", self.adapter_key)
            pages.append(self._parsed(self._sent(connection, HttpRequest(
                "GET", connection.format_url(str(following)),
                headers=self._headers(first["header"]),
            ))))
        if self.pagination_mode not in (None, "link"):
            _refuse(self.adapter_key, f"pagination_mode '{self.pagination_mode}' is not implemented")
        return [pages]

    def _headers(self, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        """One request's headers: its header fields, then the job's defaults.

        Defaults go LAST, so a default wins a same-named field -- the precedence
        the connection's HeadersPolicy gave them before they moved onto the
        request (backlog 838).
        """
        merged = {**fields, **dict(self.default_headers or {})}
        return merged or None

    def _sent(self, connection: Any, request: Any) -> Any:
        """The response to one request, retried while its status is a listed one."""
        attempts = max(int(self.retry_max_attempts or 1), 1)
        for attempt in range(1, attempts + 1):
            response = connection.send_request(request)
            status = int(response.status_code)
            if 200 <= status < 300:
                return response
            if status not in self.retry_status_codes or attempt == attempts:
                _logger.debug("http operation %s: refused -- HTTP %d after %d attempt(s)",
                              self.adapter_key, status, attempt)
                raise HttpTransportError(
                    f"http operation {self.adapter_key}: HTTP {status} after {attempt} attempt(s)."
                )
            if self.retry_backoff != "exponential":
                _refuse(self.adapter_key, f"retry_backoff '{self.retry_backoff}' is not implemented")
            wait = _retry_after(response.headers.get("Retry-After"), attempt)
            _logger.debug(
                "http operation %s: HTTP %d, attempt %d of %d, waiting %.1fs",
                self.adapter_key, status, attempt, attempts, wait,
            )
            _sleep(wait)
        _logger.debug("http operation %s: refused -- no attempt was made", self.adapter_key)
        raise HttpTransportError(f"http operation {self.adapter_key}: no attempt was made.")

    def _parsed(self, response: Any) -> Any:
        """The response body as data: json, or the CSV owner's read of a csv text."""
        if self.response_format == "json":
            return response.json()
        if self.response_format == "text" and self.response_text_format == "csv":
            return read_csv_text(response.text())
        _refuse(self.adapter_key, f"response_format '{self.response_format}'"
                                  f"{' / ' + self.response_text_format if self.response_text_format else ''}"
                                  " is not implemented")
        return None

    def _rows(self, record: dict[str, Any], pages: list[Any]) -> list[dict[str, Any]]:
        """The output rows of one input across its answer pages."""
        rows: list[dict[str, Any]] = []
        for page in pages:
            error = self._path(self.response_error_path, page)
            if error is not None:
                rows.append(self._row(record, None, "error", str(error)))
            # A CSV record is located by its header, as the CSV owner read it.
            headers = list(page.header_fields) if self.response_format == "text" else None
            for found in self._records(page):
                rows.append(self._row(record, found, "ok", None, headers))
        if rows:
            return rows
        message = next((m for m in (self._path(self.response_message_path, page) for page in pages)
                        if m is not None), None)
        return [self._row(record, None, "no_match", None if message is None else str(message))]

    def _records(self, page: Any) -> list[Any]:
        """The response records of one page."""
        if self.response_format == "text":
            if self.response_records_path:
                _refuse(self.adapter_key, "response_records_path on a text response is not implemented")
            return list(page.rows)
        found = _search(self.response_records_path, page) if self.response_records_path else page
        if found is None:
            return []
        return list(found) if isinstance(found, list) else [found]

    def _path(self, path: Optional[str], page: Any) -> Any:
        """A configured error or message path on one page; None where none is set."""
        if not path:
            return None
        if self.response_format == "text":
            _refuse(self.adapter_key, "an error or message path on a text response is not implemented")
        return _search(path, page)

    def _row(self, record: dict[str, Any], found: Any, status: str,
             message: Optional[str], headers: Optional[list[str]] = None) -> dict[str, Any]:
        """One output row: the input record, the response fields, status and message."""
        row = dict(record)
        for name, path in self.response_fields:
            row[name] = None if found is None else self._field(found, path, headers)
        row[OPERATION_STATUS] = status
        row[OPERATION_MESSAGE] = message
        return row

    def _field(self, found: Any, path: str, headers: Optional[list[str]]) -> Any:
        """One response field of one record: a jmespath, or a CSV column by its header.

        A path the record does not have answers None, for a CSV header as for a
        JSON member.
        """
        if headers is not None:
            position = headers.index(path) if path in headers else None
            return (found.fields[position]
                    if position is not None and position < len(found.fields) else None)
        return _search(path, found)


def _refuse(adapter_key: str, reason: str) -> None:
    """Log the refusal's context, then raise it."""
    _logger.debug("http operation %s: refused -- %s", adapter_key, reason)
    raise ConfigError(f"http operation {adapter_key}: {reason}.")


def _search(path: Optional[str], data: Any) -> Any:
    """A configured ``$`` path evaluated against data with jmespath; None where absent.

    ``$`` is the data itself, ``$.a.b`` a member path and ``$[0]`` an element.
    """
    if not path:
        return None
    expression = path[1:] if path.startswith("$") else path
    expression = expression[1:] if expression.startswith(".") else expression
    return jmespath.search(expression or "@", data)


def _retry_after(header: Any, attempt: int) -> float:
    """The wait before the next attempt: Retry-After when stated, else 1, 2, 4 ... s.

    Retry-After is honoured in both of its forms, delta-seconds and an HTTP-date.
    """
    if header:
        text = str(header).strip()
        if text.isdigit():
            return float(text)
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)
    return float(2 ** (attempt - 1))
