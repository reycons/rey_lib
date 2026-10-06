"""The OpenFIGI v3 mapping adapter: identifiers in, FIGI candidates out (backlog 667).

    records -> POST mapping (batches of jobs) -> one row per candidate

OpenFIGI's API contract, applied to a load's records, on the generic layers:
requests go through ``rey_lib.web_utils`` (``HttpRequest``, the connection's
``format_url`` and ``send_request``) and the transform is ``HTTPTransform``.
Everything here is OpenFIGI's behaviour, not HTTP's:

- **Batching.** Jobs go in batches of ``batch_size``, in input order.
- **Positional correlation.** Result *i* belongs to job *i*; a response of any
  other length is refused, never guessed.
- **One row per candidate.** An ambiguous match is kept whole -- never
  collapsed to its first candidate -- and a no-match or an error is still one
  row, so every input record stays visible in the output.
- **Rate limits.** ``ratelimit-remaining`` of 0 waits ``ratelimit-reset``
  before the next request; a 429 waits ``Retry-After`` (else
  ``ratelimit-reset``, else 60 s); a 500 or 503 backs off exponentially. Each
  repeats the same batch up to ``max_retries``. Repeating is safe because
  mapping is a read-only lookup.
- **Interpretation.** A job's ``warning`` is a no-match row and its ``error``
  an error row; a configuration fault (400, 401, 404, 405, 406, 413, 415) is a
  ConfigError; a provider failure that outlasts the retries is OpenFigiError.

The output schema is FIXED and declared here, so ``output_columns`` answers from
names alone and never sends a request. /v3/search and /v3/filter, the paginated
endpoints, are not used.

Options
-------
id_column (required)  the input column holding the identifier
id_type (required)    the OpenFIGI idType of every job, e.g. TICKER, ID_ISIN
job                   constant job properties, from the documented list only
batch_size            jobs per request, 1..100, default 5 -- OpenFIGI documents
                      5 or 10 without a key and 100 with one; a keyed
                      connection's configuration sets 100
max_retries           repeats of one batch after 429, 500 or 503; default 3
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from rey_lib.data.http_transform import HttpTransformAdapter, http_transform_adapter
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import get_logger
from rey_lib.web_utils import HttpConnection, HttpRequest, HttpResponse

__all__ = ["OUTPUT_COLUMNS", "OpenFigiAdapter", "OpenFigiError"]

_logger = get_logger(__name__)

#: The optional job properties OpenFIGI documents for /v3/mapping.
_JOB_PROPERTIES: frozenset[str] = frozenset({
    "exchCode", "micCode", "currency", "marketSecDes", "securityType",
    "securityType2", "includeUnlistedEquities", "optionType", "strike",
    "contractSize", "coupon", "expiration", "maturity", "stateCode",
})

#: Each documented candidate property, and the column it is written to.
_CANDIDATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("figi", "figi_figi"),
    ("name", "figi_name"),
    ("ticker", "figi_ticker"),
    ("exchCode", "figi_exch_code"),
    ("marketSector", "figi_market_sector"),
    ("securityType", "figi_security_type"),
    ("securityType2", "figi_security_type2"),
    ("compositeFIGI", "figi_composite_figi"),
    ("shareClassFIGI", "figi_share_class_figi"),
    ("securityDescription", "figi_security_description"),
    ("metadata", "figi_metadata"),
)

#: The columns every output row adds after its input record's own, in order.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "figi_job", "figi_status", "figi_candidate_count", "figi_message",
    *(column for _, column in _CANDIDATE_COLUMNS),
)

_DEFAULT_BATCH_SIZE = 5
_MAX_BATCH_SIZE = 100
_DEFAULT_MAX_RETRIES = 3

#: Statuses that are a configuration fault, and what each says.
_CONFIGURATION_STATUSES: dict[int, str] = {
    400: "invalid payload",
    401: "the API key is invalid",
    404: "invalid url",
    405: "invalid HTTP method",
    406: "unsupported Accept type",
    413: "payload too large -- batch_size exceeds this key's job limit",
    415: "invalid Content-Type",
}

#: Statuses OpenFIGI says to retry.
_RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 503})

#: Waiting when a 429 names no wait, and the exponential back-off for 500/503.
_RATE_LIMIT_WAIT_SECONDS = 60.0
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0

#: How the adapter waits. Replaced in tests so nothing waits there.
_sleep = time.sleep


class OpenFigiError(AppError):
    """OpenFIGI failed in a way retrying did not cure, or broke its own contract."""


@dataclass(frozen=True)
class _Settings:
    """The validated options."""

    id_column: str
    id_type: str
    job: dict[str, Any]
    batch_size: int
    max_retries: int

    @classmethod
    def of(cls, options: Mapping[str, Any]) -> "_Settings":
        """Validate the options, before any request is sent.

        Raises:
            ConfigError: Naming the option that is missing or malformed.
        """
        id_column = str(options.get("id_column") or "").strip()
        if not id_column:
            raise ConfigError("openfigi: option id_column is required.")
        id_type = str(options.get("id_type") or "").strip()
        if not id_type:
            raise ConfigError("openfigi: option id_type is required.")
        job = options.get("job") or {}
        if not isinstance(job, Mapping):
            raise ConfigError("openfigi: option job must be a mapping of job properties.")
        unknown = sorted(set(job) - _JOB_PROPERTIES)
        if unknown:
            raise ConfigError(
                f"openfigi: option job names properties OpenFIGI does not document: "
                f"{', '.join(unknown)}."
            )
        batch_size = _whole(options.get("batch_size"), _DEFAULT_BATCH_SIZE, "batch_size")
        if not 1 <= batch_size <= _MAX_BATCH_SIZE:
            raise ConfigError(
                f"openfigi: option batch_size must be 1..{_MAX_BATCH_SIZE}, got {batch_size}."
            )
        max_retries = _whole(options.get("max_retries"), _DEFAULT_MAX_RETRIES, "max_retries")
        if max_retries < 0:
            raise ConfigError("openfigi: option max_retries must not be negative.")
        return cls(id_column, id_type, dict(job), batch_size, max_retries)


def _whole(value: Any, default: int, name: str) -> int:
    """An integer option, or its default when absent."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"openfigi: option {name} must be a whole number.")
    return value


def _seconds(response: HttpResponse, header: str) -> Optional[float]:
    """A header's value as seconds, or None when absent or not a number."""
    try:
        return float(response.headers.get(header, ""))
    except ValueError:
        return None


def _text(value: Any) -> str:
    """A candidate property as cell text."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


@http_transform_adapter("openfigi")
class OpenFigiAdapter(HttpTransformAdapter):
    """OpenFIGI v3 /mapping: each record's identifier mapped to its FIGI candidates."""

    def output_columns(self, input_columns: list[str]) -> list[str]:
        """The input columns, then :data:`OUTPUT_COLUMNS`. Sends nothing."""
        return [*input_columns, *OUTPUT_COLUMNS]

    def apply(
        self,
        records: list[dict[str, Any]],
        connection: HttpConnection,
        options: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Map every record's identifier, batch by batch, in input order.

        Raises:
            ConfigError: An option is malformed, id_column is not a column of
                the records, or OpenFIGI answered with a configuration fault.
            OpenFigiError: A retryable failure outlasted max_retries, a 4xx/5xx
                OpenFIGI does not document arrived, or a response broke the
                one-result-per-job contract.
        """
        settings = _Settings.of(options)
        if records and settings.id_column not in records[0]:
            raise ConfigError(
                f"openfigi: option id_column '{settings.id_column}' is not a column "
                "of the records."
            )
        produced: list[dict[str, Any]] = []
        wait = 0.0
        batches = range(0, len(records), settings.batch_size)
        for number, start in enumerate(batches, 1):
            batch = records[start:start + settings.batch_size]
            sent = [
                (start + offset, record) for offset, record in enumerate(batch)
                if str(record.get(settings.id_column) or "").strip()
            ]
            results: dict[int, Mapping[str, Any]] = {}
            if sent:
                if wait:
                    self._wait(wait, "rate limit window spent")
                _logger.debug(
                    "openfigi: batch %d of %d, %d job(s)", number, len(batches), len(sent),
                )
                answers, wait = self._send(connection, settings, [
                    {**settings.job, "idType": settings.id_type,
                     "idValue": str(record[settings.id_column]).strip()}
                    for _, record in sent
                ])
                results = {position: answer for (position, _), answer in zip(sent, answers)}
            for offset, record in enumerate(batch):
                position = start + offset
                produced.extend(_rows(record, position, results.get(position), settings))
        return produced

    def _send(
        self,
        connection: HttpConnection,
        settings: _Settings,
        jobs: list[dict[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], float]:
        """One batch's results, and how long to wait before the next request."""
        for attempt in range(settings.max_retries + 1):
            response = connection.send_request(
                HttpRequest("POST", connection.format_url("mapping"), json=jobs),
            )
            status = response.status_code
            if status == 200:
                answers = response.json()
                if not isinstance(answers, list) or len(answers) != len(jobs):
                    raise OpenFigiError(
                        f"openfigi: {len(jobs)} job(s) sent but the response is not "
                        "one result per job; results cannot be correlated."
                    )
                remaining = _seconds(response, "ratelimit-remaining")
                reset = _seconds(response, "ratelimit-reset") or 0.0
                return answers, (reset if remaining == 0 else 0.0)
            if status in _CONFIGURATION_STATUSES:
                raise ConfigError(
                    f"openfigi: status {status}, {_CONFIGURATION_STATUSES[status]}."
                )
            if status in _RETRY_STATUSES and attempt < settings.max_retries:
                self._wait(_retry_wait(response, attempt), f"status {status}")
                continue
            break
        raise OpenFigiError(
            f"openfigi: status {status} after {settings.max_retries} retr(ies)."
            if status in _RETRY_STATUSES else
            f"openfigi: status {status} is not one OpenFIGI documents for mapping."
        )

    @staticmethod
    def _wait(seconds: float, reason: str) -> None:
        """Wait, saying why. No identifiers, no values."""
        _logger.debug("openfigi: waiting %.1f s (%s)", seconds, reason)
        _sleep(seconds)


def _retry_wait(response: HttpResponse, attempt: int) -> float:
    """How long to wait before repeating a batch after a retryable status.

    Retry-After first, as OpenFIGI states it. A 429 otherwise follows the
    rate-limit window; a 500 or 503 backs off exponentially.
    """
    stated = _seconds(response, "Retry-After")
    if stated is not None:
        return stated
    if response.status_code == 429:
        reset = _seconds(response, "ratelimit-reset")
        return reset if reset is not None else _RATE_LIMIT_WAIT_SECONDS
    return min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt))


def _rows(
    record: Mapping[str, Any],
    position: int,
    answer: Optional[Mapping[str, Any]],
    settings: _Settings,
) -> list[dict[str, Any]]:
    """The output rows for one input record: one per candidate, else one."""
    def row(status: str, count: int, message: str, candidate: Mapping[str, Any]) -> dict:
        return {
            **record,
            "figi_job": position,
            "figi_status": status,
            "figi_candidate_count": count,
            "figi_message": message,
            **{column: _text(candidate.get(name)) for name, column in _CANDIDATE_COLUMNS},
        }

    if answer is None:
        return [row("no_match", 0, f"no identifier in {settings.id_column}", {})]
    if not isinstance(answer, Mapping):
        raise OpenFigiError(f"openfigi: job {position} result is not an object.")
    if "error" in answer:
        return [row("error", 0, _text(answer["error"]), {})]
    candidates = answer.get("data")
    if candidates:
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, Mapping) for candidate in candidates
        ):
            raise OpenFigiError(f"openfigi: job {position} data is not a list of objects.")
        return [row("matched", len(candidates), "", candidate) for candidate in candidates]
    if "warning" in answer or candidates == []:
        return [row("no_match", 0, _text(answer.get("warning")), {})]
    raise OpenFigiError(f"openfigi: job {position} result has no data, warning or error.")
