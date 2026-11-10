"""#474: feed declarations construct canonical inputs and resolve through one execution."""
from types import SimpleNamespace

import pytest

from rey_lib.config.config_utils import Namespace
from rey_lib.load import load_operation
from rey_lib.load.source import Source
from rey_lib.load.target import Target
from rey_lib.load.transform import Transform


@pytest.mark.parametrize("explicit", [False, True])
def test_configured_file_uses_canonical_resolution_and_preserves_identity_schema(
    tmp_path, monkeypatch, explicit,
):
    file = tmp_path / "prepared.jsonl"
    file.write_text('{"amount":"1,234.50","code":"x"}\n', encoding="utf-16")
    definition = Namespace({"name": "feed", "paths": {"prepared": str(tmp_path)},
        "transforms": [{"name": "orders", "version": "v1", "file_type": "JSONL",
            "encoding": "utf-16", "columns": [{"name": "amount", "transform": {"type": "numeric"}},
                                               {"name": "code"}]}]})
    movements = Namespace({"success": [], "failure": []})
    load = Namespace({"name": "orders", "version": "v1", "source": "prepared",
                      "pickup_pattern": "*.jsonl", "movements": movements,
                      "load": {"connection": "warehouse", "destination_table": "s.orders",
                               "create_destination_table": True}})
    # Use the existing path resolver's result; the cutover does not change path syntax.
    monkeypatch.setattr(load_operation, "resolve_path", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(load_operation, "log_input_discovered", lambda *_a, **_k: None)
    reached = []
    def transfer(source, transform, target, **context):
        assert str(source.path) == str(file)
        assert source.encoding == "utf-16"
        rows = [{"amount": "1,234.50", "code": "x"}]
        assert transform.transform(rows) is rows  # No second application of numeric rules.
        assert transform.columns == ["amount", "code"]
        assert target.connection == "warehouse" and target.name == "orders"
        assert context["loader"].create_destination is True
        assert context["movements"] is movements
        assert context["paths"] is definition.paths
        reached.append(context)
        return 1
    monkeypatch.setattr(load_operation, "_load_one_file", transfer)
    configured = load_operation._configured_load(SimpleNamespace(), object(), definition, load,
                                                 explicit_files=[file] if explicit else None)
    assert isinstance(configured.transform, Transform)
    assert isinstance(configured.target, Target)
    assert isinstance(configured.source_for(file), Source)
    assert configured.load(object()) == 1
    assert len(reached) == 1
