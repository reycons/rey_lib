"""The two extractions a binding check compares, and the traps in each.

Backlog 80. A binding is YAML naming a routine, its parameters and its result
mode, and nothing compared any of the three against the catalog until now.
These cover the halves that are easy to get wrong and impossible to notice:
a mis-zipped argument list publishes a plausible index that is silently wrong,
and a binding's ``output`` block read as parameters buries every real finding
under noise.

Both subjects are pure functions over catalog arrays and config dicts, so they
are exercised here without a database. The published result is checked against
the live catalog separately, by the record that ships with them.
"""

from __future__ import annotations

import pytest

from rey_lib.config.config_namespace import Namespace
from rey_lib.db.postgres_utils import _routine_parameters
from rey_lib.repository_map.bridge_index import (
    _bindings,
    _bound_parameters,
    _seam_observations,
)


# ---------------------------------------------------------------------------
# The catalog side: proallargtypes, and where pronargdefaults applies
# ---------------------------------------------------------------------------

def test_all_arguments_are_published_not_only_the_input_ones() -> None:
    """OUT arguments are invisible through proargtypes, and decide result mode.

    Shape of control.f_batch_step_begin: nine IN then two OUT. proargtypes and
    pg_get_function_identity_arguments carry the nine; only proallargtypes sees
    the eleven, and the two OUT are the whole reason the routine yields a row.
    """
    parameters = _routine_parameters(
        arg_types=["text"] * 11,
        arg_names=[f"p{i}" for i in range(1, 10)] + ["o_one", "o_two"],
        arg_modes=["i"] * 9 + ["o", "o"],
        n_defaults=7,
    )

    assert len(parameters) == 11
    assert [p["member_mode"] for p in parameters[-2:]] == ["OUT", "OUT"]
    # The trailing SEVEN INPUT arguments, which are ordinals 3..9 -- not
    # ordinals 5..11, which is what counting back through the OUT rows gives.
    assert [p["ordinal"] for p in parameters if p["has_default"]] == [3, 4, 5, 6, 7, 8, 9]


def test_defaults_are_counted_over_inputs_not_over_the_whole_list() -> None:
    """A TABLE column is not an argument a caller may omit.

    Shape of control.f_file_profile_get: three IN then one TABLE result column.
    Applying pronargdefaults to the last N of the complete list marks the TABLE
    column optional and leaves the genuinely optional input looking required --
    so the check would demand a parameter the caller already passes.
    """
    parameters = _routine_parameters(
        arg_types=["bigint", "character varying", "bigint", "jsonb"],
        arg_names=["p_file_manifest_id", "p_representation",
                   "in_parent_batch_step_id", "profile"],
        arg_modes=["i", "i", "i", "t"],
        n_defaults=1,
    )

    assert [p["member_mode"] for p in parameters] == ["IN", "IN", "IN", "TABLE"]
    assert [p["member_name"] for p in parameters if p["has_default"]] == [
        "in_parent_batch_step_id"
    ]


def test_inout_is_input_capable_and_can_be_defaulted() -> None:
    """INOUT appears in both catalog arrays and counts toward pronargdefaults.

    Shape of control.p_data_profile_ins: fourteen IN then three INOUT outputs.
    All seventeen are input-capable, so the eleven defaults land on ordinals
    7..17 -- which is what the routine's own signature declares.
    """
    parameters = _routine_parameters(
        arg_types=["text"] * 17,
        arg_names=[f"p{i}" for i in range(1, 15)]
                  + ["o_data_profile_id", "o_file_type_id", "o_batch_step_id"],
        arg_modes=["i"] * 14 + ["b", "b", "b"],
        n_defaults=11,
    )

    assert [p["member_mode"] for p in parameters[-3:]] == ["INOUT"] * 3
    assert [p["ordinal"] for p in parameters if p["has_default"]] == list(range(7, 18))


def test_a_null_modes_array_means_every_argument_is_in() -> None:
    """Postgres omits proargmodes entirely when nothing is OUT, INOUT or TABLE.

    Shape of control.f_file_manifest_get, which is also RETURNS SETOF: absence
    is a statement about the arguments, not missing data, and reading it as
    missing would leave every plain routine with no modes at all.
    """
    parameters = _routine_parameters(
        arg_types=["bigint", "bigint"],
        arg_names=["p_file_manifest_id", "in_parent_batch_step_id"],
        arg_modes=None,
        n_defaults=1,
    )

    assert [p["member_mode"] for p in parameters] == ["IN", "IN"]
    assert [p["member_name"] for p in parameters if p["has_default"]] == [
        "in_parent_batch_step_id"
    ]


def test_a_routine_taking_nothing_publishes_nothing() -> None:
    """No arguments is a real shape, not an error."""
    assert _routine_parameters([], None, None, 0) == []


def test_unnamed_arguments_do_not_shorten_the_list() -> None:
    """A missing name must not drop the argument it belongs to.

    No routine in this estate has unnamed arguments today, so nothing would
    notice a zip that shortened -- and the parameter count is exactly what the
    published index is checked on.
    """
    parameters = _routine_parameters(
        arg_types=["bigint", "text"], arg_names=None, arg_modes=None, n_defaults=0,
    )

    assert len(parameters) == 2
    assert [p["member_name"] for p in parameters] == ["", ""]


# ---------------------------------------------------------------------------
# The binding side: input is parameters, output is not
# ---------------------------------------------------------------------------

def test_only_the_input_block_names_routine_parameters() -> None:
    """``output`` carries run-context keys, and publishing them is noise.

    ``{variable, load_to_ctx}`` name where a result is put, not what the
    routine takes. Read as parameters they report `variable` as unknown on
    nearly every binding in the estate, burying the real findings.
    """
    bound = _bound_parameters({
        "name": "write_run_log_record",
        "routine": "control.p_run_log_ins",
        "result_mode": "scalar_result",
        "output": {"variable": "run_log_id", "load_to_ctx": "run_log_id"},
        "input": {"p_run_id": "run_id", "p_record_type": "record_type"},
    })

    assert sorted(bound) == ["p_record_type", "p_run_id"]


def test_the_legacy_inputs_spelling_is_read() -> None:
    """The map loader accepts both, so the extractor must.

    A binding written the legacy way would otherwise publish no parameters --
    and a binding with no parameters passes every assertion silently.
    """
    assert _bound_parameters({"inputs": {"p_one": "one"}}) == ["p_one"]


def test_a_binding_with_no_input_block_publishes_no_parameters() -> None:
    """Absent and empty are the same answer, and neither is an error."""
    assert _bound_parameters({"name": "x", "routine": "control.f_x"}) == []
    assert _bound_parameters({"input": None}) == []


def test_a_loaded_binding_is_a_namespace_and_is_read_the_same_way() -> None:
    """The type the extractor actually meets, not the one tests reach for.

    A loaded ``input`` block is a config Namespace: it has ``keys`` and ``get``
    but NO ``__iter__``, and its ``__getitem__`` is attribute access, so
    iterating it raises on the first integer index. Every test above uses dict
    literals and would pass against an extractor that published nothing at all
    -- which is exactly what an ``isinstance(..., dict)`` test did.
    """
    binding = Namespace({
        "name": "insert_file_manifest",
        "routine": "control.p_file_manifest_ins",
        "result_mode": "dataset_result",
        "output": {"variable": "file_manifest_id"},
        "input": {"p_path": "path", "p_file_name": "file_name"},
    })

    # The premise: this is not a dict and cannot be iterated.
    assert not isinstance(binding.input, dict)
    with pytest.raises(TypeError):
        list(binding.input)

    assert sorted(_bound_parameters(binding)) == ["p_file_name", "p_path"]


def test_the_legacy_call_type_spelling_yields_a_result_mode() -> None:
    """A binding written the legacy way HAS a mode; it lacks the modern key.

    procedure_map resolves ``call_type`` into a result mode at load time, so
    reading only ``result_mode`` publishes an empty string -- and an empty mode
    is checked by nothing, because db_binding_vw cannot compare a declaration
    that is not there. Five of local's bindings are written this way.
    """
    ctx = type("Ctx", (), {"procedure_maps": [{
        "name": "control",
        "routine_bindings": [
            {"name": "end_batch", "routine": "control.p_batch_end",
             "call_type": "procedure_no_return", "input": {"p_batch_id": "batch_id"}},
            {"name": "start_step", "routine": "control.f_batch_step_start",
             "call_type": "function_with_return", "input": {"p_batch_id": "batch_id"}},
            # The modern key wins where both somehow appear.
            {"name": "modern", "routine": "control.f_x",
             "call_type": "procedure_no_return", "result_mode": "dataset_result"},
        ],
    }]})()

    bindings, _ = _bindings(ctx, "local")
    modes = {b["binding_name"]: b["result_mode"] for b in bindings}

    assert modes["end_batch"] == "no_return"
    assert modes["start_step"] == "scalar_result"
    assert modes["modern"] == "dataset_result"


def test_the_dispatch_method_is_recorded_with_each_observation(tmp_path) -> None:
    """Which method reached a binding is what decides its result_mode.

    ``_call_rows`` requires ``dataset_result``; ``_call`` forbids it, because a
    dataset_result binding leaves ``outputs`` empty and ``_call`` reads only
    that. The seam has always filtered on the method and then discarded it, so a
    binding could be read one way and declared the other with nothing to notice.
    """
    source = tmp_path / "control.py"
    source.write_text(
        "class Control:\n"
        "    def a(self):\n"
        "        return self._call('insert_file_manifest', {})\n"
        "    def b(self):\n"
        "        return self._call_rows('get_files_to_classify', {})\n"
        "    def c(self, binding_name):\n"
        "        return self._call_rows(binding_name, {})\n",
        encoding="utf-8",
    )
    seam = {"seam": "control_dispatch", "repository": "rey_lib",
            "relative_path": "control.py", "receiver": "self",
            "methods": ("_call", "_call_rows")}

    found, status = _seam_observations(source, seam)

    by_binding = {o["observed_binding"]: o for o in found}
    assert by_binding["insert_file_manifest"]["dispatch_method"] == "_call"
    assert by_binding["get_files_to_classify"]["dispatch_method"] == "_call_rows"

    # A non-literal binding name stays unresolved and keeps its method: the
    # method is known even when the binding is not, and a partial coverage
    # status is what says the extractor could not read everything.
    unresolved = [o for o in found if o["resolution"] == "unresolved"]
    assert [o["dispatch_method"] for o in unresolved] == ["_call_rows"]
    assert status == "partial"


def test_bindings_carry_their_result_mode_and_their_parameters() -> None:
    """The whole binding side of the comparison, from one procedure map."""
    ctx = type("Ctx", (), {"procedure_maps": [{
        "name": "control",
        "routine_bindings": [{
            "name": "insert_file_manifest",
            "routine": "control.p_file_manifest_ins",
            "result_mode": "scalar_result",
            "input": {"p_path": "path", "p_recorded_at": "recorded_at"},
            "output": {"variable": "file_manifest_id"},
        }],
        "sql_bindings": [{"name": "some_sql", "result_mode": "dataset_result"}],
    }]})()

    bindings, parameters = _bindings(ctx, "local")

    routine = next(b for b in bindings if b["target_kind"] == "routine")
    assert routine["result_mode"] == "scalar_result"
    assert routine["schema_name"] == "control"
    assert routine["object_name"] == "p_file_manifest_ins"

    # SQL names no database object and must not be given a fabricated one.
    sql = next(b for b in bindings if b["target_kind"] == "sql")
    assert (sql["schema_name"], sql["object_name"]) == ("", "")

    # Parameters come from the routine binding only, and carry the identity
    # they are joined back on.
    assert [p["parameter_name"] for p in parameters] == ["p_path", "p_recorded_at"]
    assert {p["binding_name"] for p in parameters} == {"insert_file_manifest"}
    assert {p["installation"] for p in parameters} == {"local"}
