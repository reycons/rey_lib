"""Static path evidence: cleanup continuations, expression order and coverage."""
from pathlib import Path

import pytest

from rey_lib.repository_map.control_flow import python_control_flow, javascript_control_flow
from rey_lib.repository_map.extractors import extract_executable_references


def graph(tmp_path, text, language="Python"):
    path = tmp_path / ("sample.py" if language == "Python" else "sample.ts")
    path.write_text(text)
    acquire = python_control_flow if language == "Python" else javascript_control_flow
    rows = [f.to_dict() for f in acquire(path, language, path.name)]
    nodes = {r["node_key"]: r for r in rows if r["record_type"] == "control_flow_node"}
    edges = [r for r in rows if r["record_type"] == "control_flow_edge"]
    coverage = [r for r in rows if r["record_type"] == "control_flow_coverage"]
    return path, nodes, edges, coverage


def reaches(nodes, edges, start, finish, *, forbidden=()):
    todo = [start]
    seen = set(forbidden)
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        if current == finish:
            return True
        seen.add(current)
        todo.extend(e["to_key"] for e in edges if e["from_key"] == current)
    return False


@pytest.mark.parametrize("exit_text,kind", [("return value()", "return"), ("raise Fault()", "raise"),
                                            ("break", "break"), ("continue", "continue")])
def test_python_abrupt_exit_runs_finally_before_its_destination(tmp_path, exit_text, kind):
    text = f'''def run():
    while ready():
        try:
            {exit_text}
        finally:
            cleanup()
    after()
'''
    _, nodes, edges, _ = graph(tmp_path, text)
    abrupt = next(k for k, n in nodes.items() if n["kind"] == kind)
    outgoing = [e for e in edges if e["from_key"] == abrupt]
    assert len(outgoing) == 1
    cleanup = outgoing[0]["to_key"]
    assert nodes[cleanup]["callee"] == "cleanup"
    assert f":{kind}" in cleanup
    dest = next(e["to_key"] for e in edges if e["from_key"] == cleanup and e["kind"] == "next")
    while nodes[dest]["kind"] == "statement":
        dest = next(e["to_key"] for e in edges if e["from_key"] == dest and e["kind"] == "next")
    expected = {"return": "exit", "raise": "exception_exit", "break": "call", "continue": "loop"}
    assert nodes[dest]["kind"] == expected[kind]
    if kind == "break":
        assert nodes[dest]["callee"] == "after"


def test_finally_return_overrides_pending_exception(tmp_path):
    _, nodes, edges, coverage = graph(tmp_path, '''def run():
    try:
        raise Fault()
    finally:
        return replacement()
''')
    raised = next(k for k, n in nodes.items() if n["kind"] == "raise")
    replacement = next(k for k,n in nodes.items() if n.get("callee") == "replacement" and ":raise" in k)
    exit_node = next(k for k,n in nodes.items() if n["kind"] == "exit")
    assert reaches(nodes, edges, raised, replacement)
    assert reaches(nodes, [e for e in edges if e["kind"] != "exception"], raised, exit_node)
    assert coverage[0]["status"] == "complete"


def test_nested_calls_keep_evaluation_order_and_join_existing_call_identity(tmp_path):
    path, nodes, edges, _ = graph(tmp_path, 'def run():\n    outer(first(), second(inner()))\n')
    calls = {n["callee"]: key for key,n in nodes.items() if n["kind"] == "call"}
    normal = [e for e in edges if e["kind"] != "exception"]
    for before, after in [("first", "inner"), ("inner", "second"), ("second", "outer")]:
        assert reaches(nodes, normal, calls[before], calls[after])
        assert not reaches(nodes, normal, calls[after], calls[before])
    refs = extract_executable_references(path,"Python",path.name)
    for key in calls.values():
        n=nodes[key]
        assert len([r for r in refs if (r.source_line,r.source_column,r.to,r.edge_kind)==
                    (n["source_line"],n["source_column"],n["callee"],n["call_edge_kind"])]) == 1
    assert [r.to_dict() for r in python_control_flow(path,"Python",path.name)] == [
        r.to_dict() for r in python_control_flow(path,"Python",path.name)]


def test_true_and_false_paths_do_not_become_a_source_sorted_sequence(tmp_path):
    _, nodes, edges, coverage = graph(tmp_path, '''def run(flag):
    if flag:
        yes()
    else:
        no()
    done()
''')
    calls={n["callee"]: k for k,n in nodes.items() if n["kind"]=="call"}
    normal=[e for e in edges if e["kind"]!="exception"]
    assert not reaches(nodes,normal,calls["yes"],calls["no"])
    assert reaches(nodes,normal,calls["yes"],calls["done"])
    assert reaches(nodes,normal,calls["no"],calls["done"])
    assert coverage[0]["status"]=="complete"


@pytest.mark.parametrize("body", ['match x:\n        case 1: hit()', 'with resource():\n        hit()',
                                  'values = [f(x) for x in source]'])
def test_python_unsupported_semantics_are_explicitly_partial(tmp_path,body):
    _, nodes, _, coverage=graph(tmp_path,'def run():\n    '+body+'\n')
    assert coverage[0]["status"]=="partial"
    assert coverage[0]["reason"]
    assert any(n["kind"] in ("boundary","cleanup") for n in nodes.values())


@pytest.mark.parametrize("language",["JavaScript","TypeScript","TSX"])
def test_javascript_cleanup_and_branch_paths(tmp_path,language):
    path,nodes,edges,coverage=graph(tmp_path, '''function run(flag) {
  try {
    if (flag) return first();
    throw fail();
  } finally {
    cleanup();
  }
}''',language)
    for kind in ("return","raise"):
        node=next(k for k,n in nodes.items() if n["kind"]==kind)
        target=next(e["to_key"] for e in edges if e["from_key"]==node)
        assert nodes[target]["callee"]=="cleanup"
        assert f":{kind}" in target
    assert coverage[0]["status"]=="complete"
    refs=extract_executable_references(path,language,path.name)
    for n in nodes.values():
        if n["kind"]=="call":
            assert any((r.source_line,r.source_column,r.to,r.edge_kind)==
                       (n["source_line"],n["source_column"],n["callee"],n["call_edge_kind"]) for r in refs)


def test_javascript_unsupported_switch_is_not_reported_complete(tmp_path):
    _,_,_,coverage=graph(tmp_path,'function run(x) { switch(x) { case 1: hit(); } }','TypeScript')
    assert coverage[0]["status"]=="partial"
    assert 'switch_statement' in coverage[0]["reason"]


def test_flow_facts_survive_the_existing_scan_to_staging_pipeline(tmp_path):
    from rey_lib.repository_map.code_index import _indexed
    from rey_lib.repository_map.writer import RepositoryMap
    from tests.test_code_index import _header, _file
    from tests.test_code_index_database import _Adapter
    from rey_lib.repository_map.code_index_database import CodeIndexDatabaseWriter
    path=tmp_path/'sample.py'
    path.write_text('def run():\n    return operation()\n')
    records=[r.to_dict() for r in python_control_flow(path,'Python',path.name)]
    indexed=_indexed('example',RepositoryMap(header=_header('example'),records=[_file(path.name),*records]))
    assert indexed.control_flow_nodes and indexed.control_flow_edges
    assert indexed.control_flow_coverage[0]['owner_qualified_name']=='run'
    adapter=_Adapter()
    CodeIndexDatabaseWriter(adapter,None).replace_index([indexed])
    tables={table: count for schema,table,count,cols in adapter.inserts}
    assert tables['control_flow_node_stage']==len(indexed.control_flow_nodes)
    assert tables['control_flow_edge_stage']==len(indexed.control_flow_edges)
    assert tables['control_flow_coverage_stage']==1
    assert adapter.statements[-1]=='CALL code.p_publish(true)'


def test_javascript_plain_syntax_snapshot_handles_destructuring_and_nested_ternary(tmp_path):
    _,nodes,edges,coverage=graph(tmp_path,'''function run(value) {
      const {first, second} = get(value);
      return first ? second ? a() : b() : c();
    }''','TypeScript')
    assert coverage[0]['status']=='complete'
    assert sum(n['kind']=='condition' for n in nodes.values())==2
    assert {n['callee'] for n in nodes.values() if n['kind']=='call'}=={'get','a','b','c'}


def test_normal_and_exceptional_cleanup_do_not_share_the_same_continuation(tmp_path):
    _,nodes,edges,_=graph(tmp_path,'''def run():
    try:
        work()
    finally:
        cleanup()
    after()
''')
    normal=next(k for k,n in nodes.items() if n.get('callee')=='cleanup' and k.endswith(':normal'))
    exceptional=next(k for k,n in nodes.items() if n.get('callee')=='cleanup' and k.endswith(':raise'))
    after=next(k for k,n in nodes.items() if n.get('callee')=='after')
    normal_edges=[e for e in edges if e['kind']!='exception']
    assert reaches(nodes,normal_edges,normal,after)
    assert not reaches(nodes,normal_edges,exceptional,after)


def test_python_starred_argument_runs_before_earlier_written_keyword(tmp_path):
    _,nodes,edges,_=graph(tmp_path,'def run():\n    sink(key=keyword(), *positional())\n')
    calls={n['callee']:key for key,n in nodes.items() if n['kind']=='call'}
    normal=[e for e in edges if e['kind']!='exception']
    assert reaches(nodes,normal,calls['positional'],calls['keyword'])
    assert not reaches(nodes,normal,calls['keyword'],calls['positional'])


def test_literal_nul_in_javascript_source_is_visible_in_indexed_excerpt(tmp_path):
    text='function run(value) { const key = `${value}\x00suffix`; return key; }'
    path,nodes,_,coverage=graph(tmp_path,text,'TypeScript')
    assert path.read_text()==text  # indexed source is never rewritten
    assert '\x00' in path.read_text()
    excerpt=next(n for n in nodes.values() if n['kind']=='statement')
    assert r'\u0000' in excerpt['text']
    assert not any('\x00' in value for n in nodes.values() for value in n.values()
                   if isinstance(value,str))
    start,end=excerpt['source_column'],excerpt['end_column']
    assert '\x00' in path.read_text()[start:end]
    assert coverage[0]['status']=='complete'


def test_activity_label_is_the_declarations_own_docstring_not_invented_text(tmp_path):
    _,nodes,_,_=graph(tmp_path, """def run():
    \"\"\"Load the records described by the selected objects.

    The rest is implementation documentation.
    \"\"\"
    perform()
""")
    assert next(n['text'] for n in nodes.values() if n['kind']=='entry') == 'Load the records described by the selected objects.'


def test_compiler_proves_validation_calls_before_partial_comprehension(tmp_path):
    _,nodes,edges,coverage=graph(tmp_path, """def run(source, transform, target):
    problems = [name for name, found in (
        ('Source', source.validate()),
        ('Transform', transform.validate()),
        ('Target', target.validate()),
    ) if found]
    after()
""")
    calls={n['callee']:key for key,n in nodes.items() if n['kind']=='call'}
    normal=[e for e in edges if e['kind']!='exception']
    assert reaches(nodes,normal,calls['source.validate'],calls['transform.validate'])
    assert reaches(nodes,normal,calls['transform.validate'],calls['target.validate'])
    assert not reaches(nodes,normal,calls['target.validate'],calls['source.validate'])
    assert coverage[0]['status']=='partial' and 'ListComp' in coverage[0]['reason']


def test_generator_body_calls_are_not_executed_when_its_iterator_is_created(tmp_path):
    _,nodes,_,coverage=graph(tmp_path, 'def run():\n    iterator = (deferred(x) for x in outer())\n')
    assert {n['callee'] for n in nodes.values() if n['kind']=='call'}=={'outer'}
    assert coverage[0]['status']=='partial'
