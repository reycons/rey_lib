"""Column transformation, as the transform object that owns it.

    Source object -> TRANSFORM OBJECT -> Destination object

**This is where the estate's YAML column-transformation behaviour lives.** It
was a set of functions beside a file reader -- ``files/transformer.py`` -- so
only a configured file feed could reach it, and the transform object in the
load triple applied nothing at all. The behaviour did not change when it moved
here; its OWNER did.

What the object owns, and owns together
---------------------------------------
- **the declaration**: which source field each output column comes from, what
  it is called, and what is applied to it;
- **the execution** of those rules against records.

A class that read its declaration on every call would be a namespace with
arguments, not an object -- so the declaration arrives once, at construction,
and ``transform(records)`` takes records and nothing else. That is also what
``DataTransform`` has always asked for.

What it is NOT
--------------
**It knows nothing about files.** A header line, a file name, a format token
and resolving a positional or fixed-width field are file parsing and stay at
the file boundary. Every record reaching here is KEYED -- which every
``DataFile`` already guarantees, headerless formats included -- so a source is
named by key and there is no positional branch to take.

**It does not read secrets from the declaration it applies.** They are a
dependency of the object, supplied at construction by whatever trusted thing
built it. A declaration is a description of work; letting one carry the keys
it is decrypted with would make the description a credential.

Transformation types
--------------------
constant, context, date, datetime, time, numeric, regex_extract, regex_date,
prefix_map, strip_parens_suffix, encrypt, file_hash, not_blank, and ``hash``
over a fixed set of output columns. Each is declared per column under
``transform:`` and each is implemented below.

``AUTHORABLE_STARTERS`` names the subset a SURFACE may offer, with a starting
declaration each. Every type above stays legal in a declaration however it was
written; the subset is about what a surface hands an author, not about what
this module executes.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import date, datetime, time
from typing import Any, Optional

from rey_lib.data.data_transform import DeclaredTransform
from rey_lib.data.date_formats import to_strptime_format
from rey_lib.data.errors import TransformError
from rey_lib.logs import get_logger

__all__ = [
    "AUTHORABLE_STARTERS",
    "OUTPUT_DATATYPES",
    "ColumnTransform",
    "authorable_starters",
    "is_exported",
]

_logger = get_logger(__name__)


#: A starting declaration per transform type a SURFACE may offer.
#:
#: **Authoring aids, not defaults.** Nothing below reads this: it changes no
#: runtime behaviour and no reading of an omitted key. A declaration is only
#: ever what its author actually wrote, and a starter is what an author is
#: handed before they write it.
#:
#: It sits beside the dispatch because the dispatch owns the types. A surface
#: holding its own list would be a second answer about what a transform is,
#: free to drift from the one that executes.
#:
#: **``encrypt`` IS DELIBERATELY ABSENT, and its absence here is not a claim
#: that it is invalid.** ``_transform_encrypt`` resolves ``key_env`` against a
#: secrets map supplied at construction, so a declaration naming one is only
#: as good as the store the thing that built it can reach. Existing
#: declarations keep using it through the trusted paths that already supply
#: that map. What is missing is the rule for a declaration arriving from a
#: BROWSER -- which secret namespace it may name, and how those names are
#: offered without the values -- and until that rule exists, offering the
#: starter would advertise a capability that is not whole.
AUTHORABLE_STARTERS: dict[str, dict[str, Any]] = {
    "constant": {"type": "constant", "value": ""},
    "context": {"type": "context", "value": "ctx.row_num"},
    # TWO FORMATS, because they are two questions and only one of them was
    # being offered. `format` READS the value; `output_format` WRITES it, and
    # without it a rule falls back to the ANSI spelling. An author who wanted
    # dates written as MM/dd/yyyy had to know the second key existed and type it
    # in, because the starter mentioned only the first.
    #
    # Each is seeded with the ANSI default that rule already falls back to, so
    # the key is visible and editable and changes nothing for anyone who leaves
    # it alone.
    "date": {
        "type": "date", "format": "MM/dd/yyyy", "output_format": "yyyy-MM-dd",
    },
    "datetime": {
        "type": "datetime", "format": "yyyy-MM-dd HH:mm:ss",
        "output_format": "yyyy-MM-dd HH:mm:ss",
    },
    "time": {
        "type": "time", "format": "HH:mm:ss", "output_format": "HH:mm:ss",
    },
    "numeric": {"type": "numeric", "strip_chars": ","},
    "regex_extract": {"type": "regex_extract", "source": "", "pattern": "", "group": 1},
    "regex_date": {
        "type": "regex_date", "source": "", "pattern": "", "format": "yyMMdd", "group": 1,
    },
    "prefix_map": {"type": "prefix_map", "source": "", "prefixes": {}, "default": "Other"},
    "strip_parens_suffix": {"type": "strip_parens_suffix"},
    "file_hash": {"type": "file_hash"},
    "not_blank": {"type": "not_blank"},
    "hash": {"type": "hash", "hash_type": "sha256", "columns": []},
}


#: The output datatypes a declaration may state, as LOGICAL types.
#:
#: **What the output is intended to BE.** Its sibling questions are answered
#: elsewhere and stay there: ``transform`` is how a value gets there, and how a
#: logical type is spelled physically is the destination's.
#:
#: NEUTRAL, not a server's vocabulary. A declaration naming ``DATETIME2`` would
#: be one engine's spelling written into a description of work, and would be
#: wrong everywhere else it was read.
#:
#: NOT the profiling vocabulary either. ``detect_datatype`` answers what a value
#: LOOKS LIKE in a source -- email, ssn, name -- which is a different question
#: from what a destination column should be.
#:
#: ``boolean`` IS ABSENT, and deliberately: every member here renders as a string
#: this estate already emits, and boolean is the one that would mean inventing a
#: physical type in shared code. It waits for somewhere a provider can answer.
OUTPUT_DATATYPES: tuple[str, ...] = (
    "text", "integer", "decimal", "date", "datetime",
)


def is_exported(column: dict[str, Any]) -> bool:
    """Whether this entry contributes a column to the output.

    ABSENT MEANS EXPORTED. Every declaration written before the property
    existed, and every one that simply does not care, keeps its meaning.

    Args:
        column: One entry of a declaration's ``columns``.

    Returns:
        False only where the entry states ``export: false``.
    """
    return column.get("export", True) is not False


def authorable_starters() -> dict[str, dict[str, Any]]:
    """The starting declarations a surface may offer, copied.

    COPIED, because a caller that edited what it was given would edit the
    starter every later caller is handed. A starter is a beginning, and one
    that carried a previous author's edits would not be.

    DEEPLY copied, because some of them hold a container -- ``prefixes`` and
    ``columns`` -- and a shallow copy would share the one thing an author is
    most likely to put something in.

    Returns:
        Each authorable type mapped to a fresh starting declaration.
    """
    return {
        name: deepcopy(declaration)
        for name, declaration in AUTHORABLE_STARTERS.items()
    }


class ColumnTransform(DeclaredTransform):
    """A declaration, and the rules it states applied to records.

    **The transform object of a load that has one.** Its sibling
    ``IdentityTransform`` passes records through, which is right for a load
    whose records a transform stage already produced; this one is for a load
    that was handed raw records and told what to do with them.

    Everything about the OUTPUT -- which columns, in what order, typed how --
    is ``DeclaredTransform``'s and is not restated here. What is here is the
    application.

    Built once per transformation operation. A caller that constructed one per
    record would have a namespace with arguments again, and the object could
    own neither its row counter nor anything else across a run.
    """

    def __init__(
        self,
        declaration: dict[str, Any],
        *,
        context: Any = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        """Hold the rules, and the trusted things applying them needs.

        Args:
            declaration: The transform declaration, as plain data. Its
                ``columns`` are the output columns -- each naming its
                ``source``, its ``name`` and what is applied -- and its
                ``row_filter`` decides which records are kept at all.
            context: The run's context, for ``context`` and ``constant``
                values that reference it.
            secrets: Named secrets the declaration's rules may use.
                **SUPPLIED, NEVER READ FROM THE DECLARATION.** A declaration
                describes work; one that carried its own keys would be a
                credential wearing the shape of a description, and this object
                would have no way to tell an operator's declaration from
                anyone else's.

        The declared columns and their types are derived here, once, so the
        schema half this shares with the identity transform answers from the
        same declaration the application uses -- rather than from a second
        copy a caller resolved separately.
        """
        # THE EXPORTED ONES, which is not every declared one. An entry marked
        # `export: false` is still computed -- see `transform_record` -- and is
        # simply not part of what comes out, so it is absent from the schema
        # this hands upward and from every answer derived from it.
        columns = [
            str(one.get("name", ""))
            for one in declaration.get("columns") or []
            if isinstance(one, dict) and one.get("name") and is_exported(one)
        ]
        super().__init__(
            column_transforms={
                str(one["name"]): one["transform"]
                for one in declaration.get("columns") or []
                if isinstance(one, dict)
                and one.get("name")
                and isinstance(one.get("transform"), dict)
                and one["transform"]
            },
            # NONE ONLY WHERE THE DECLARATION NAMES NO ENTRIES AT ALL, which is
            # what None has always meant here: nothing was declared, so the
            # records pass through. A declaration whose entries are every one of
            # them unexported is NOT that -- it declared columns and exports
            # none of them -- and collapsing the two would answer a mistake by
            # silently carrying the whole source, which is the one response that
            # looks like success.
            columns=columns if declaration.get("columns") else None,
            # The declared OUTPUT types, by column, for the entries that state
            # one. Held beside the transforms because both are things the
            # declaration says about a produced column.
            column_datatypes={
                str(one["name"]): str(one["datatype"])
                for one in declaration.get("columns") or []
                if isinstance(one, dict)
                and one.get("name")
                and one.get("datatype")
            },
        )
        self.declaration = declaration
        self.context = context
        self.secrets = secrets or {}
        #: Records seen, 1-based, for the ``ctx.row_num`` context value. The
        #: object's own, because a caller passing it in per record is what
        #: made this a function taking configuration.
        self._row_num = 0

    def columns_for_names(self, actual: list[str]) -> list[str]:
        """What this transform PRODUCES from a source with these names.

        **THE OVERRIDE THAT A RENAME REQUIRES.** The inherited rule compares
        the declared columns against the names it is given and refuses a
        mismatch -- which is exactly right for the identity transform, where
        what is produced IS what arrived, so a declaration naming anything
        else is a declaration that has drifted from its file.

        Here they are not the same list and are not meant to be: ``actual`` is
        the SOURCE's fields and the declared columns are the OUTPUT. A load
        that renames ``a`` to ``identifier`` would be refused by the inherited
        comparison for doing the one thing it was told to do.

        So there is nothing to compare. The declaration says what comes out.

        Args:
            actual: The source's own field names, in order. Read only to
                answer when a declaration names no columns at all.

        Returns:
            The declared output columns, in order.
        """
        if self.columns is None:
            return actual
        return list(self.columns)

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the declaration to these records.

        Records only: the rules, the context and the secrets were settled when
        this object was built.

        A record the declaration's ``row_filter`` rejects is DROPPED, not
        transformed -- blank lines, repeated headers, footers and summary rows
        are what that exists for -- so the result may be shorter than the
        input, and a caller counting rows must count what comes back.

        Returns:
            The produced records, in order.

        Raises:
            TransformError: When a column's rule fails, naming the column.
        """
        produced: list[dict[str, Any]] = []
        for record in records:
            one = self.transform_record(record)
            if one is not None:
                produced.append(one)
        return produced

    def transform_record(self, record: dict[str, Any]) -> Optional[dict[str, Any]]:
        """One record, or None where the row filter rejects it.

        **PUBLIC BESIDE THE LIST FORM, and not a second way to do the same
        thing.** ``transform`` is the contract and is what a caller holding
        every record uses. A caller STREAMING records -- reading a file row by
        row so it can record which row failed and carry on -- has a different
        need, and asking it to build a one-element list per row to get an
        answer per row would be the list contract pretending to serve it.

        Both go through here, so there is one implementation.

        HASH COLUMNS ARE DEFERRED to a second pass, because a hash is computed
        ACROSS output columns and cannot be taken before they exist. That
        ordering is the declaration's meaning, not an implementation detail:
        a hash declared anywhere but last still hashes finished values.

        Returns:
            The produced record, or None when the row filter rejects it.

        Raises:
            TransformError: When a column's rule fails, naming the column.
        """
        # COUNTED BEFORE THE FILTER, because this is the record's position in
        # what was read -- which is what `ctx.row_num` has always meant and
        # what an operator reads it back as. Counting survivors instead would
        # renumber every row after the first blank line.
        self._row_num += 1
        if not _passes_row_filter(record, self.declaration):
            return None

        out: dict[str, Any] = {}
        deferred: list[dict] = []

        for col_cfg in self.declaration.get("columns", []):
            name = col_cfg["name"]
            transform_cfg = col_cfg.get("transform") or {}

            if transform_cfg.get("type") == "hash":
                deferred.append(col_cfg)
                continue

            out[name] = _apply_transform_v2(
                name,
                # NAMED BY KEY. Every record reaching here is keyed -- a
                # DataFile guarantees it for every format, headerless ones
                # included -- so resolving a source is a lookup and there is
                # no positional branch. Where a field comes from in a FILE is
                # the file boundary's question and was answered before this.
                record.get(str(col_cfg["source"]))
                if col_cfg.get("source") is not None else None,
                transform_cfg,
                out,
                record,
                self.secrets,
                row_num=self._row_num,
                ctx=self.context,
            )

        for col_cfg in deferred:
            out[col_cfg["name"]] = _compute_hash(out, col_cfg["transform"])

        # PROJECTED LAST, and that ordering is the whole meaning of `export`.
        # Every entry was computed into `out` above, so an unexported column is
        # available to everything declared after it -- a `hash` over an
        # intermediate value is exactly what that is for. Dropping it at the
        # point it was computed would have made `export` an execution switch
        # wearing an output-selection name.
        if self.columns is None:
            return out
        return {name: out[name] for name in self.columns if name in out}


def _resolve_context_value(value_str: str, ctx: Any = None, row_num: int = 0) -> Any:
    """Resolve a ``ctx.*`` reference string to its runtime value."""
    if value_str == "ctx.row_num":
        return row_num
    if value_str.startswith("ctx.") and ctx is not None:
        current = ctx
        for part in value_str[4:].split("."):
            current = getattr(current, part, None)
            if current is None:
                return ""
        return current if current is not None else ""
    return value_str


def _resolve_constant_value(value: Any, ctx: Any = None) -> Any:
    """Resolve ``{ctx_field}`` tokens in constant transform values."""
    if not isinstance(value, str) or ctx is None:
        return value
    return value.format_map(_CtxFormatMap(ctx))


class _CtxFormatMap(dict):
    """Format-map adapter that resolves token names against ctx attributes."""

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self._ctx = ctx

    def __missing__(self, key: str) -> str:
        return str(getattr(self._ctx, key, ""))

def _apply_transform_v2(
    db_col: str,
    value: Any,
    transform_cfg: dict,
    out: dict[str, Any],
    raw_row: Any,
    secrets: dict[str, str],
    row_num: int = 0,
    ctx: Any = None,
) -> Any:
    """Transform dispatcher for the new list-based column shape."""
    if not transform_cfg:
        # PASS-THROUGH KEEPS THE VALUE IT WAS GIVEN, typed. A column nobody
        # declared a rule for is carried to the destination as it came, and a
        # destination with a schema for it wants the value rather than a
        # rendering of it.
        return value.strip() if isinstance(value, str) else value

    transform_type = transform_cfg.get("type", "")

    # ALREADY THE THING THE RULE WAS GOING TO PARSE FOR.
    #
    # A driver hands back a `datetime`, and the rule's job is to produce a
    # formatted date from one. Rendering it to text so a text parser can build
    # the same object back is a round trip that can only lose: `str()` on a
    # timezone-aware value with microseconds gives
    # `2026-09-05 12:23:15.001322-04:00`, which no format in the fallback list
    # matches -- so a value that needed no parsing at all failed to parse.
    #
    # Taken directly instead. Nothing is guessed, nothing is re-read, and the
    # declared `output_format` still decides how it is written.
    if isinstance(value, (datetime, date, time)):
        formatted = _format_temporal(value, transform_type, transform_cfg)
        if formatted is not _NOT_TEMPORAL:
            return formatted

    # A RULE READS TEXT, so anything else is given text.
    #
    # Every rule below guards its input with `isinstance(value, str) else ""`.
    # That held while the only source was a FILE, where each field arrives as
    # text already. A DATABASE source hands back what its driver holds, and the
    # guard turned each of them into an empty string -- so a rule failed, or
    # with `allow_blank: true` silently nulled the column, over a value that was
    # perfectly good.
    #
    # Rendered ONCE here rather than in fourteen rules, and only for a column
    # whose declaration asks for one -- the pass-through above is untouched, so
    # what a load writes for an undeclared column does not change.
    if value is not None and not isinstance(value, str):
        value = str(value)

    try:
        if transform_type == "constant":
            return _resolve_constant_value(transform_cfg.get("value"), ctx=ctx)

        if transform_type == "context":
            return _resolve_context_value(
                transform_cfg.get("value", ""), ctx=ctx, row_num=row_num,
            )

        if transform_type == "date":
            return _transform_date(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "datetime":
            return _transform_datetime(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "time":
            return _transform_time(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "numeric":
            return _transform_numeric(
                (value or "").strip() if isinstance(value, str) else "", transform_cfg,
            )

        if transform_type == "regex_extract":
            return _transform_regex_extract(raw_row, transform_cfg)

        if transform_type == "prefix_map":
            return _transform_prefix_map(db_col, out, raw_row, transform_cfg)

        if transform_type == "strip_parens_suffix":
            return _transform_strip_parens((value or "") if isinstance(value, str) else "")

        if transform_type == "regex_date":
            return _transform_regex_date(raw_row, transform_cfg)

        if transform_type == "encrypt":
            return _transform_encrypt(value, raw_row, transform_cfg, secrets)

        if transform_type == "file_hash":
            return _resolve_context_value("ctx.file_checksum", ctx=ctx)

        if transform_type == "not_blank":
            return out.get(db_col, "")

        raise TransformError(
            f"Unknown transform type '{transform_type}' for column '{db_col}'",
            column=db_col,
        )

    except TransformError as exc:
        if not getattr(exc, "column", ""):
            exc.column = db_col
        raise

#: Returned where the value is temporal but the RULE is not about time.
#:
#: A sentinel rather than None, because None is a legitimate answer from every
#: rule here -- a blank the declaration allows -- and the two must not be read
#: alike.
_NOT_TEMPORAL = object()


def _format_temporal(value: Any, transform_type: str, cfg: dict) -> Any:
    """Write an already-temporal value the way this rule was going to write it.

    The rules for ``date``, ``datetime`` and ``time`` all do the same two
    things: read a value, then render it through ``output_format`` or the ANSI
    default. A value that is already a ``date``, ``datetime`` or ``time`` has
    done the first half, so only the second is left.

    Narrowed where the rule asks for less than the value holds -- a ``date``
    rule over a timestamp takes the date, a ``time`` rule takes the time -- and
    that is the rule's own instruction rather than a loss: a column declared as
    a date is a date.

    Args:
        value: A ``date``, ``datetime`` or ``time``.
        transform_type: The declared rule.
        cfg: That rule's declaration, read for ``output_format``.

    Returns:
        The formatted text, or ``_NOT_TEMPORAL`` where this rule is not one of
        the three -- an ``encrypt`` over a timestamp still wants text.
    """
    if transform_type == "date":
        taken = value.date() if isinstance(value, datetime) else value
        if isinstance(taken, time):
            return _NOT_TEMPORAL
        return _apply_output_format(taken, cfg, _ANSI_DATE_FMT)

    if transform_type == "datetime":
        if isinstance(value, time):
            return _NOT_TEMPORAL
        taken = value if isinstance(value, datetime) else datetime(
            value.year, value.month, value.day,
        )
        return _apply_output_format(taken, cfg, _ANSI_DATETIME_FMT)

    if transform_type == "time":
        taken = value.time() if isinstance(value, datetime) else value
        if not isinstance(taken, time):
            return _NOT_TEMPORAL
        return _apply_output_format(taken, cfg, _ANSI_TIME_FMT)

    return _NOT_TEMPORAL


def _raw_text(raw_row: Any, source: str, default: Any = "") -> str:
    """One named field of the raw record, as text a rule can read.

    **THE SAME REASON THE DISPATCHER RENDERS ITS VALUE**, one level along. Three
    rules -- `regex_extract`, `regex_date` and `prefix_map` -- do not use the
    value they were handed; they go back to the raw record for a field their own
    declaration names. So the dispatcher's rendering never reached them, and a
    database source's `datetime` arrived here to have `.strip()` called on it:

        AttributeError: 'datetime.datetime' object has no attribute 'strip'

    which is a 500 with no column named rather than a fault a reader can act on.

    Absent and blank both answer with the default, as they did before.
    """
    held = raw_row.get(source, default) if hasattr(raw_row, "get") else default
    if held is None:
        return ""
    return held.strip() if isinstance(held, str) else str(held).strip()


def _passes_row_filter(raw_row: dict[str, str], file_type_cfg: dict) -> bool:
    """
    Return True if the row passes the configured row filter.

    A row that fails the filter is silently discarded — this handles blank
    lines, header repetitions, footers, and summary rows.

    Parameters
    ----------
    raw_row : dict[str, str]
        Raw CSV row.
    file_type_cfg : dict
        File type config containing an optional row_filter section.

    Returns
    -------
    bool
        True if the row should be processed, False if it should be discarded.
    """
    row_filter = file_type_cfg.get("row_filter")
    if not row_filter:
        return True

    column      = row_filter.get("column", "")
    filter_type = row_filter.get("type", "")
    value       = _raw_text(raw_row, column)

    if filter_type == "date":
        fmt = to_strptime_format(row_filter.get("format", "%m/%d/%Y"))
        return _try_parse_date(value, fmt) is not None

    if filter_type == "not_blank":
        return bool(value)

    # Unknown filter type — pass through rather than silently drop rows.
    _logger.debug("Unknown row_filter type '%s' — passing row through.", filter_type)
    return True

# ANSI standard output formats — used when output_format is not configured.
_ANSI_DATE_FMT:     str = "%Y-%m-%d"
_ANSI_DATETIME_FMT: str = "%Y-%m-%d %H:%M:%S"
_ANSI_TIME_FMT:     str = "%H:%M:%S"


def _apply_output_format(
    parsed: date | datetime | time | None,
    cfg:    dict,
    ansi_fmt: str,
) -> Optional[str]:
    """Format a parsed date/datetime/time to a string.

    Uses ``output_format`` from config when present (token syntax supported),
    otherwise falls back to the ANSI standard format for the type.
    Returns None when ``parsed`` is None.
    """
    if parsed is None:
        return None
    raw_out = cfg.get("output_format")
    fmt     = to_strptime_format(raw_out) if raw_out else ansi_fmt
    return parsed.strftime(fmt)

# ---------------------------------------------------------------------------
# Private — individual transform implementations
# ---------------------------------------------------------------------------
def _transform_date(value: str, cfg: dict) -> Optional[str]:
	"""Parse a date string using the configured format with fallback support.

	Tries ``format`` first. If that fails, tries ``fallback_formats`` from the
	field config (for uncommon or feed-specific formats), then the built-in
	unambiguous fallbacks. Ambiguous formats (e.g. day/month vs month/day)
	must be declared explicitly via ``fallback_formats`` — they are never
	auto-detected.

	Output defaults to ANSI standard ``yyyy-MM-dd``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``MM/dd/yyyy``)
	output_format    : str  — output string format (default ANSI ``yyyy-MM-dd``)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty date value and allow_blank is False.")

	fmt    = to_strptime_format(cfg.get("format", "%m/%d/%Y"))
	result = _try_parse_date(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_date(value, to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%m/%d/%y",              # US short year
			"%m/%d/%Y",              # US long year
			"%Y-%m-%d",              # ISO 8601 date
			"%Y%m%d",                # compact ISO
			"%Y/%m/%d",              # ISO slash
			"%Y-%m-%dT%H:%M:%S",    # ISO 8601 datetime
			"%Y-%m-%d %H:%M:%S",    # ISO datetime space
			"%m/%d/%Y %H:%M:%S",    # US datetime
			"%d-%b-%Y",              # 14-May-2026
			"%d-%b-%y",              # 14-May-26
			"%d %b %Y",              # 14 May 2026
			"%b %d, %Y",             # May 14, 2026
		):
			result = _try_parse_date(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as date with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_DATE_FMT)


def _transform_datetime(value: str, cfg: dict) -> Optional[str]:
	"""Parse a datetime string — preserves both date and time components.

	Output defaults to ANSI standard ``yyyy-MM-dd HH:mm:ss``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``yyyy-MM-dd HH:mm:ss``)
	output_format    : str  — output string format (default ANSI)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty datetime value and allow_blank is False.")

	fmt    = to_strptime_format(cfg.get("format", "yyyy-MM-dd HH:mm:ss"))
	result = _try_parse_datetime(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_datetime(value, to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%Y-%m-%dT%H:%M:%S",    # ISO 8601
			"%Y-%m-%d %H:%M:%S",    # ISO space
			"%Y-%m-%dT%H:%M:%S.%f", # ISO with microseconds
			"%Y-%m-%d %H:%M:%S.%f", # ISO space with microseconds
			"%m/%d/%Y %H:%M:%S",    # US datetime
			"%m/%d/%y %H:%M:%S",    # US short year
			"%Y%m%d%H%M%S",         # compact
		):
			result = _try_parse_datetime(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as datetime with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_DATETIME_FMT)


def _transform_time(value: str, cfg: dict) -> Optional[str]:
	"""Parse a time-of-day string — discards any date component.

	Output defaults to ANSI standard ``HH:mm:ss``. Override with
	``output_format`` using the same token syntax as ``format``.

	Config keys
	-----------
	format           : str  — input parse format (default ``HH:mm:ss``)
	output_format    : str  — output string format (default ANSI)
	fallback_formats : list — additional formats to try before built-in fallbacks
	allow_blank      : bool — return None on empty instead of raising
	"""
	value = value.strip()

	if not value:
		if cfg.get("allow_blank", False):
			return None
		raise TransformError("Empty time value and allow_blank is False.")

	fmt    = to_strptime_format(cfg.get("format", "HH:mm:ss"))
	result = _try_parse_time(value, fmt)

	if result is None:
		field_fallbacks = cfg.get("fallback_formats") or []
		for fallback in field_fallbacks:
			result = _try_parse_time(value, to_strptime_format(fallback))
			if result is not None:
				break

	if result is None:
		for fallback in (
			"%H:%M:%S",      # 19:20:52
			"%H:%M",         # 19:20
			"%I:%M:%S %p",   # 07:20:52 AM
			"%I:%M %p",      # 07:20 AM
			"%H%M%S",        # 192052 compact
		):
			result = _try_parse_time(value, fallback)
			if result is not None:
				break

	if result is None:
		raise TransformError(
			f"Cannot parse '{value}' as time with format '{fmt}'."
		)

	return _apply_output_format(result, cfg, _ANSI_TIME_FMT)


def _transform_numeric(value: str, cfg: dict) -> Optional[float]:
    """
    Strip unwanted characters from a string and cast to float.

    Handles parenthesised negatives: (1234.56) → -1234.56.

    Parameters
    ----------
    value : str
        Raw string value.
    cfg : dict
        Transform config. Keys: strip_chars (optional — chars to remove).

    Returns
    -------
    Optional[float]
        Parsed float, or None if the value is blank or unparseable.
    """
    value = value.strip()
    if not value:
        return None

    strip_chars = cfg.get("strip_chars", "")
    if strip_chars:
        for ch in strip_chars:
            value = value.replace(ch, "")

    value = value.strip()

    # Parenthesised negatives are common in broker exports.
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    try:
        return float(value)
    except ValueError:
        _logger.debug("Cannot parse '%s' as numeric — returning None.", value)
        return None


def _transform_regex_extract(raw_row: dict[str, str], cfg: dict) -> Any:
    """
    Extract a capture group from a source field using a regex pattern.

    Parameters
    ----------
    raw_row : dict
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source   — source column name in the raw row
            pattern  — regex pattern with capture group(s)
            group    — capture group index (default 1)
            strip    — strip whitespace from result (default True)
            cast_to  — optional: 'float'/'double' or 'int'/'integer'

    Returns
    -------
    Any
        Captured group value, cast to the configured type if specified.
        Returns the original value if no match; returns None on cast failure.
    """
    source      = cfg.get("source", "")
    pattern     = cfg.get("pattern", "")
    group       = cfg.get("group", 1)
    do_strip    = cfg.get("strip", True)
    allow_blank = cfg.get("allow_blank", False)

    value = _raw_text(raw_row, source)
    if not value or not pattern:
        return None if allow_blank else ""

    m = re.search(pattern, value)
    if not m:
        if not allow_blank:
            _logger.debug("Pattern '%s' did not match '%s'.", pattern, value)
        return None if allow_blank else value

    result = m.group(group)
    result = result.strip() if do_strip else result

    cast_to = cfg.get("cast_to", "")
    if cast_to in ("float", "double"):
        try:
            return float(result) if result else None
        except ValueError:
            _logger.debug("Cannot cast '%s' to float — returning None.", result)
            return None
    if cast_to in ("int", "integer"):
        try:
            return int(result) if result else None
        except ValueError:
            _logger.debug("Cannot cast '%s' to int — returning None.", result)
            return None

    return result


def _transform_prefix_map(
    db_col: str,
    out: dict[str, Any],
    raw_row: dict[str, str],
    cfg: dict,
) -> str:
    """
    Map a field value to a normalised value by matching prefixes longest-first.

    Prefixes are tested longest-first so more specific prefixes take
    precedence over shorter ones. Matching is case-insensitive when
    case_insensitive is True in config.

    If strip_from is set, the matched prefix is removed from the named
    output column (typically 'description') as a side effect.

    Parameters
    ----------
    db_col : str
        The output column being mapped (typically 'action').
    out : dict
        Current output row — modified in-place if strip_from is set.
    raw_row : dict
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source           — source column name
            prefixes         — dict of prefix → normalised value
            strip_from       — output column to strip the prefix from
            case_insensitive — bool (default True)
            default          — value when no prefix matches

    Returns
    -------
    str
        Normalised value, or default if no prefix matches.
    """
    source           = cfg.get("source", db_col)
    prefixes         = cfg.get("prefixes", {})
    strip_from       = cfg.get("strip_from", "")
    case_insensitive = cfg.get("case_insensitive", True)
    default          = cfg.get("default", "Other")

    raw_value = _raw_text(raw_row, source, out.get(db_col, ""))
    compare   = raw_value.upper() if case_insensitive else raw_value

    # Sort prefixes longest-first so more specific entries win.
    for prefix in sorted(prefixes.keys(), key=len, reverse=True):
        test = prefix.upper() if case_insensitive else prefix
        if compare.startswith(test):
            if strip_from and strip_from in out:
                suffix          = raw_value[len(prefix):].strip()
                out[strip_from] = _transform_strip_parens(suffix)
            return prefixes[prefix]

    _logger.debug(
        "No prefix matched for value '%s' — using default '%s'.",
        raw_value, default,
    )
    return default


def _transform_regex_date(raw_row: dict[str, str], cfg: dict) -> Optional[str]:
    """
    Extract a date string from a source field via regex then parse it.

    Combines regex extraction and date parsing in a single transform —
    useful when a date is embedded within a larger string such as an
    option symbol (e.g. AAPL240119C00150000 → 2024-01-19).

    Output defaults to ANSI standard ``yyyy-MM-dd``. Override with
    ``output_format`` using the same token syntax as ``format``.

    Parameters
    ----------
    raw_row : dict[str, str]
        Original raw CSV row.
    cfg : dict
        Transform config. Keys:
            source        — source column name
            pattern       — regex with optional capture group(s)
            format        — input format for the extracted date string
            output_format — output string format (default ANSI ``yyyy-MM-dd``)
            group         — capture group index (default 1); use 0 for full match
            allow_blank   — if True, return None on no-match instead of raising

    Returns
    -------
    Optional[str]
        Formatted date string, or None if no match or allow_blank is True.

    Raises
    ------
    TransformError
        If the pattern matches but the date cannot be parsed and
        allow_blank is False.
    """
    source      = cfg.get("source", "")
    pattern     = cfg.get("pattern", "")
    fmt         = to_strptime_format(cfg.get("format", "%y%m%d"))
    group       = cfg.get("group", 1)
    allow_blank = cfg.get("allow_blank", False)

    value = _raw_text(raw_row, source)
    if not value or not pattern:
        return None

    m = re.search(pattern, value)
    if not m:
        return None

    try:
        date_str = m.group(group) if group else m.group(0)
    except IndexError:
        if allow_blank:
            return None
        raise TransformError(
            f"Pattern '{pattern}' has no group {group} in value '{value}'."
        )

    result = _try_parse_date(date_str.strip(), fmt)
    if result is None:
        if allow_blank:
            return None
        raise TransformError(
            f"Cannot parse '{date_str}' as date with format '{fmt}'."
        )
    return _apply_output_format(result, cfg, _ANSI_DATE_FMT)


def _transform_encrypt(
    value: Any,
    raw_row: dict[str, str],
    cfg: dict,
    secrets: dict[str, str],
) -> Optional[str]:
    """
    Encrypt a string value using Fernet symmetric encryption.

    Blank values are returned unchanged. The encryption key is resolved
    from the secrets dict using the key_env name specified in config.
    Requires the 'cryptography' package.

    Parameters
    ----------
    value : Any
        Plaintext value to encrypt (already mapped output value).
    raw_row : dict[str, str]
        Raw source row. Used when encrypt config also includes regex
        extraction keys (source/pattern/group/strip).
    cfg : dict
        Transform config. Keys:
            key_env — name of the environment variable holding the Fernet key.
            include_key_env — when True, output as '<key_env>:<token>'.
    secrets : dict[str, str]
        Resolved env-var values keyed by variable name.

    Returns
    -------
    Optional[str]
        Fernet token string, or '<key_env>:<token>' when include_key_env is
        True. Returns the original value if blank.

    Raises
    ------
    TransformError
        If key_env is missing, the key cannot be found, or encryption fails.
    """
    # Optional pre-extraction: when source/pattern are present, derive the
    # plaintext from raw_row first, then encrypt the extracted value.
    plaintext: Any = value
    if cfg.get("source") and cfg.get("pattern"):
        plaintext = _transform_regex_extract(raw_row, cfg)

    if plaintext is None:
        return None

    plaintext_str = str(plaintext).strip()

    # Return blank values unchanged — nothing to encrypt.
    if not plaintext_str:
        return plaintext_str

    key_env = cfg.get("key_env", "")
    if not key_env:
        raise TransformError(
            "encrypt transform requires 'key_env' to be set in the transform config."
        )

    raw_key = secrets.get(key_env)
    if not raw_key:
        raise TransformError(
            f"Encryption key '{key_env}' not found. "
            f"Ensure {key_env} is set in the environment or .env file."
        )

    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415  lazy import
    except ImportError as exc:
        raise TransformError(
            "The 'cryptography' package is required for encrypt transforms. "
            "Install it: pip install cryptography"
        ) from exc

    try:
        # Key must be bytes; encode if the env var value is a plain string.
        key    = raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key
        fernet = Fernet(key)
        token  = fernet.encrypt(plaintext_str.encode("utf-8"))
        token_text = token.decode("utf-8")

        if cfg.get("include_key_env", False):
            return f"{key_env}:{token_text}"

        return token_text
    except (TypeError, ValueError) as exc:
        raise TransformError(
            f"Fernet encryption failed: {exc}"
        ) from exc


def _transform_strip_parens(value: str) -> str:
    """
    Remove trailing parenthesised tokens from a description string.

    Applied repeatedly until no trailing parens tokens remain.
    Examples: "Buy (Cash)" → "Buy", "Foo (Bar) (Baz)" → "Foo".

    Parameters
    ----------
    value : str
        Raw description string.

    Returns
    -------
    str
        Cleaned string with trailing parenthesised tokens removed.
    """
    pattern = re.compile(r"\s*\([^)]+\)\s*$")
    result  = value.strip()
    while True:
        cleaned = pattern.sub("", result).strip()
        if cleaned == result:
            break
        result = cleaned
    return result


# ---------------------------------------------------------------------------
# Private — helpers
# ---------------------------------------------------------------------------

def _compute_hash(out: dict[str, Any], hash_cfg: dict) -> str:
    """Compute a deterministic hash over the specified output columns.

    Values are stringified and joined with '|' before hashing.  None values
    and missing keys become empty strings so the result is always stable.

    Parameters
    ----------
    out : dict[str, Any]
        The fully-transformed output row (all earlier steps already applied).
    hash_cfg : dict
        The 'hash' section from the file-type config.  Expected keys:
            name      — output column name (used by the caller, not here)
            hash_type — algorithm name accepted by hashlib (default 'sha256')
            columns   — list of output column names to include in the hash

    Returns
    -------
    str
        Hex-digest string.

    Raises
    ------
    TransformError
        If the requested hash_type is not available in hashlib.
    """
    algorithm = hash_cfg.get("hash_type", "sha256")
    columns   = hash_cfg.get("columns", [])

    parts = (str(out.get(col, "") or "") for col in columns)
    payload = "|".join(parts).encode("utf-8")

    try:
        digest = hashlib.new(algorithm, payload)
    except ValueError as exc:
        raise TransformError(
            f"Unsupported hash_type '{algorithm}': {exc}"
        ) from exc

    return digest.hexdigest()

def _try_parse_date(value: str, fmt: str) -> Optional[date]:
    """Attempt to parse a date string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _try_parse_datetime(value: str, fmt: str) -> Optional[datetime]:
    """Attempt to parse a datetime string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt)
    except ValueError:
        return None


def _try_parse_time(value: str, fmt: str) -> Optional[time]:
    """Attempt to parse a time string, returning None on failure."""
    try:
        return datetime.strptime(value.strip(), fmt).time()
    except ValueError:
        return None
