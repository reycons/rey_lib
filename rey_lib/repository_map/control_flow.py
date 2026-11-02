"""Source-derived static control flow, emitted by the existing language extractors.

No target resolution, execution or source parser lives here. The language owners
supply their existing syntax trees. Nodes name source positions and lexical roles;
cleanup copies include their continuation so abrupt exits cannot become normal ones.
"""
from __future__ import annotations

import ast
import dis
from types import CodeType
from dataclasses import dataclass
from typing import Any

__all__ = ["FlowFact", "python_control_flow", "javascript_control_flow"]

# The published v2 contract carries input evidence in commands[].
# Additional proved input bindings use the same node/edge/coverage format.
VERSION = "2"


@dataclass(frozen=True)
class FlowFact:
    """One normalized index fact; publication resolves its existing symbol owner."""
    fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.fields)


class FlowGraph:
    """One declaration's extracted nodes, continuations and coverage evidence."""
    def __init__(self, path: str, owner: str, line: int, column: int):
        self.owner = {"source_path": path, "owner_qualified_name": owner,
                      "owner_line": line, "owner_column": column}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str]] = set()
        self.reasons: set[str] = set()

    def node(self, kind: str, pos: tuple[int, int, int, int], text: str,
             context: str = "", callee: str | None = None,
             call_edge_kind: str | None = None) -> str:
        line, col, end_line, end_col = pos
        key = f"{line}:{col}:{end_line}:{end_col}:{kind}:{context}"
        self.nodes[key] = {"node_key": key, "kind": kind, "source_line": line,
                           "source_column": col, "end_line": end_line, "end_column": end_col,
                           # Text is a display excerpt, not the source-byte authority. A literal
                           # NUL is legal inside JS strings but cannot travel in SQL text.
                           # Keep it visible; the indexed file/span retains exact source.
                           "text": text.replace("\x00", r"\u0000"),
                           "callee": callee, "call_edge_kind": call_edge_kind, "commands": []}
        return key

    def edge(self, source: str, target: str, kind: str = "next") -> None:
        self.edges.add((source, target, kind))

    def facts(self) -> list[FlowFact]:
        base = self.owner
        result = []
        for key, row in sorted(self.nodes.items()):
            result.append(FlowFact({"record_type": "control_flow_node",
                "record_id": f"control_flow_node:{base['source_path']}:{base['owner_qualified_name']}:{base['owner_line']}:{base['owner_column']}:{key}",
                **base, **row}))
        for source, target, kind in sorted(self.edges):
            result.append(FlowFact({"record_type": "control_flow_edge",
                "record_id": f"control_flow_edge:{base['source_path']}:{base['owner_qualified_name']}:{base['owner_line']}:{base['owner_column']}:{source}:{kind}:{target}",
                **base, "from_key": source, "to_key": target, "kind": kind}))
        result.append(FlowFact({"record_type": "control_flow_coverage",
            "record_id": f"control_flow_coverage:{base['source_path']}:{base['owner_qualified_name']}:{base['owner_line']}:{base['owner_column']}",
            **base, "status": "partial" if self.reasons else "complete",
            "reason": "; ".join(sorted(self.reasons)), "producer_version": VERSION}))
        return result


class PythonFlow(FlowGraph):
    """Continuation-based Python control flow over the existing AST."""
    def pos(self, node: ast.AST) -> tuple[int, int, int, int]:
        return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)

    def make(self, node: ast.AST, kind: str, context: str = "", text: str | None = None) -> str:
        return self.node(kind, self.pos(node),
                         (ast.get_source_segment(self.source, node) or ast.unparse(node))
                         if text is None else text, context)

    def eager_comprehension_calls(self, node: ast.AST) -> list[ast.Call]:
        """Only the CPython-proved linear call prefix before iterator creation.

        The comprehension body stays partial. In particular a generator's
        deferred body must never be made into calls executed at construction.
        """
        span = lambda value: ((value.lineno, value.col_offset), (value.end_lineno, value.end_col_offset))
        start, finish = span(node)
        candidates = {span(call): call for call in ast.walk(node.generators[0].iter)
                      if isinstance(call, ast.Call)}
        result = []
        for instruction in self.instructions:
            pos = instruction.positions
            if pos.lineno is None or pos.col_offset is None or pos.end_lineno is None or pos.end_col_offset is None:
                continue
            location = ((pos.lineno, pos.col_offset), (pos.end_lineno, pos.end_col_offset))
            if not (start <= location[0] and location[1] <= finish):
                continue
            if (instruction.opname in ("GET_ITER", "GET_AITER", "FOR_ITER")
                or instruction.opcode in dis.hasjrel or instruction.opcode in dis.hasjabs):
                break
            if instruction.opname.startswith("CALL") and location in candidates:
                result.append(candidates[location])
        return result

    def expression(self, node: ast.AST | None, after: str, targets: dict[str, str], ctx: str) -> str:
        if node is None:
            return after
        if isinstance(node, (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.Yield, ast.YieldFrom, ast.NamedExpr)):
            self.reasons.add(type(node).__name__)
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "unknown")
            self.edge(key, targets["raise"], "exception")
            result = key
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                from rey_lib.repository_map.python_extractor import _classify_call
                for call in reversed(self.eager_comprehension_calls(node)):
                    callee, edge_kind = _classify_call(call)
                    current = self.node("call", self.pos(call), ast.get_source_segment(self.source, call)
                                        or ast.unparse(call), ctx, callee, edge_kind)
                    self.edge(current, result)
                    self.edge(current, targets["raise"], "exception")
                    result = current
            return result
        if isinstance(node, ast.Dict):
            self.reasons.add("dictionary key/value interleaving")
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "unknown")
            return key
        if isinstance(node, ast.IfExp):
            yes = self.expression(node.body, after, targets, ctx)
            no = self.expression(node.orelse, after, targets, ctx)
            return self.condition(node.test, yes, no, targets, ctx)
        if isinstance(node, ast.BoolOp):
            result = self.expression(node.values[-1], after, targets, ctx)
            for value in reversed(node.values[:-1]):
                result = (self.condition(value, result, after, targets, ctx)
                          if isinstance(node.op, ast.And)
                          else self.condition(value, after, result, targets, ctx))
            return result
        if isinstance(node, ast.Call):
            from rey_lib.repository_map.python_extractor import _classify_call
            callee, edge_kind = _classify_call(node)
            call = self.node("call", self.pos(node), ast.get_source_segment(self.source, node) or ast.unparse(node), ctx, callee, edge_kind)
            self.edge(call, after)
            self.edge(call, targets["raise"], "exception")
            # Python evaluates receiver/callee before arguments, arguments before invocation.
            # Positional/starred arguments precede keyword arguments even when
            # a star is written after a keyword. This is AST/language order,
            # deliberately NOT sorting calls by their source positions.
            args = [*node.args, *(k.value for k in node.keywords)]
            result = call
            for child in reversed([node.func, *args]):
                result = self.expression(child, result, targets, ctx)
            return result
        if isinstance(node, ast.Await):
            self.reasons.add("await scheduling/cancellation")
            key = self.make(node, "await", ctx)
            self.edge(key, after, "resume")
            self.edge(key, targets["raise"], "exception")
            return self.expression(node.value, key, targets, ctx)
        if isinstance(node, ast.Compare) and len(node.ops) > 1:
            self.reasons.add("chained comparison short-circuit")
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "unknown")
            return key
        children = list(ast.iter_child_nodes(node))
        # Atomic expressions can still raise (attribute lookup, operators, indexing).
        result = after
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.BinOp, ast.UnaryOp, ast.Compare)):
            result = self.make(node, "expression", ctx)
            self.edge(result, after)
            self.edge(result, targets["raise"], "exception")
        for child in reversed(children):
            if isinstance(child, ast.expr):
                result = self.expression(child, result, targets, ctx)
        return result

    def condition(self, node: ast.AST, yes: str, no: str, targets: dict[str, str], ctx: str) -> str:
        if isinstance(node, ast.BoolOp):
            result = self.condition(node.values[-1], yes, no, targets, ctx)
            for value in reversed(node.values[:-1]):
                result = (self.condition(value, result, no, targets, ctx)
                          if isinstance(node.op, ast.And)
                          else self.condition(value, yes, result, targets, ctx))
            return result
        branch = self.make(node, "condition", ctx)
        self.edge(branch, yes, "true")
        self.edge(branch, no, "false")
        self.edge(branch, targets["raise"], "exception")
        return self.expression(node, branch, targets, ctx)

    def sequence(self, statements: list[ast.stmt], after: str, targets: dict[str, str], ctx: str) -> str:
        result = after
        for statement in reversed(statements):
            result = self.statement(statement, result, targets, ctx)
        return result

    def statement(self, node: ast.stmt, after: str, targets: dict[str, str], ctx: str) -> str:
        if isinstance(node, ast.If):
            yes = self.sequence(node.body, after, targets, ctx)
            no = self.sequence(node.orelse, after, targets, ctx)
            return self.condition(node.test, yes, no, targets, ctx)
        if isinstance(node, (ast.While, ast.For)):
            loop = self.make(node, "loop", ctx, "while" if isinstance(node, ast.While) else "for next")
            inner = {**targets, "break": after, "continue": loop}
            body = self.sequence(node.body, loop, inner, ctx)
            exhausted = self.sequence(node.orelse, after, targets, ctx)
            if isinstance(node, ast.While):
                test = self.condition(node.test, body, exhausted, targets, ctx)
                self.edge(loop, test, "repeat")
                return loop
            self.edge(loop, body, "item")
            self.edge(loop, exhausted, "exhausted")
            self.edge(loop, targets["raise"], "exception")
            return self.expression(node.iter, loop, targets, ctx)
        if isinstance(node, ast.Try):
            # Each pending completion gets its own cleanup continuation. Abrupt
            # completion IN cleanup uses the outer targets and overrides it.
            routed = dict(targets)
            normal = after
            if node.finalbody:
                routed = {kind: self.sequence(node.finalbody, dest, targets,
                           f"{ctx}/finally@{node.lineno}:{kind}")
                          for kind, dest in targets.items()}
                normal = self.sequence(node.finalbody, after, targets,
                                       f"{ctx}/finally@{node.lineno}:normal")
            dispatch = routed["raise"]
            if node.handlers:
                dispatch = self.make(node, "exception_dispatch", ctx, "except")
                for handler in node.handlers:
                    entry = self.sequence(handler.body, normal, routed, ctx)
                    label = "except " + (ast.unparse(handler.type) if handler.type else "any")
                    self.edge(dispatch, entry, label)
                if all(handler.type is not None for handler in node.handlers):
                    self.edge(dispatch, routed["raise"], "unmatched")
            succeeded = self.sequence(node.orelse, normal, routed, ctx)
            return self.sequence(node.body, succeeded, {**routed, "raise": dispatch}, ctx)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            # __exit__ may suppress exceptions or itself raise; do not claim
            # precise context-manager behavior from a call target alone.
            self.reasons.add("context-manager exit/suppression")
            routed = {}
            for kind, dest in {**targets, "normal": after}.items():
                cleanup = self.make(node, "cleanup", f"{ctx}/with:{kind}", "context exit")
                self.edge(cleanup, dest, kind)
                self.edge(cleanup, targets["raise"], "exception")
                if kind == "raise":
                    self.edge(cleanup, after, "possibly_suppressed")
                routed[kind] = cleanup
            body = self.sequence(node.body, routed["normal"], routed, ctx)
            entry = self.make(node, "context_enter", ctx, "context enter")
            self.edge(entry, body)
            self.edge(entry, targets["raise"], "exception")
            result = entry
            for item in reversed(node.items):
                result = self.expression(item.context_expr, result, targets, ctx)
            return result
        abrupt = {ast.Return: "return", ast.Raise: "raise", ast.Break: "break", ast.Continue: "continue"}
        if type(node) in abrupt:
            kind = abrupt[type(node)]
            key = self.make(node, kind, ctx)
            self.edge(key, targets[kind], kind)
            result = key
            values = [getattr(node, "exc", None), getattr(node, "cause", None)] if kind == "raise" else [getattr(node, "value", None)]
            for value in reversed(values):
                result = self.expression(value, result, targets, ctx)
            return result
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            key = self.make(node, "definition", ctx, node.name)
            self.edge(key, after)
            if node.decorator_list or isinstance(node, ast.ClassDef) or node.args.defaults or node.args.kw_defaults:
                self.reasons.add("definition decorators/class execution")
            return key
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr,
                                 ast.Pass, ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            self.reasons.add(type(node).__name__)
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "unknown")
            self.edge(key, targets["raise"], "exception")
            return key
        key = self.make(node, "statement", ctx)
        self.edge(key, after)
        self.edge(key, targets["raise"], "exception")
        result = key
        # Assignment evaluation differs from syntactic field order: RHS first.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if any(not isinstance(t, ast.Name) for t in getattr(node, "targets", [getattr(node, "target", None)])):
                self.reasons.add("assignment target evaluation")
            return self.expression(node.value, result, targets, ctx)
        if isinstance(node, ast.AugAssign):
            self.reasons.add("augmented assignment evaluation")
        for child in reversed(list(ast.iter_child_nodes(node))):
            if isinstance(child, ast.expr):
                result = self.expression(child, result, targets, ctx)
        return result


def python_control_flow(path: Any, language: str, source_path: str | None = None) -> list[FlowFact]:
    """Extract functions already recognized by the existing Python inventory."""
    from rey_lib.repository_map.python_extractor import _parse, _recorded_bodies
    from rey_lib.files import read_text_file
    facts = []
    source = read_text_file(path)
    tree = _parse(path)
    compiled = []
    def declarations(code):
        for value in code.co_consts:
            if isinstance(value, CodeType):
                compiled.append(value)
                declarations(value)
    compiler_refused = False
    try:
        declarations(compile(tree, str(path), "exec", dont_inherit=True))
    except SyntaxError:
        # The source parser may accept a body the compiler refuses, e.g. a
        # misplaced nonlocal. No compiler-derived call facts are then claimed.
        compiler_refused = True
    for owner, name in _recorded_bodies(tree):
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        graph = PythonFlow(source_path or path.as_posix(), name, owner.lineno, owner.col_offset)
        graph.source = source
        if compiler_refused:
            graph.reasons.add("CPython compilation refused")
        candidates = [code for code in compiled if code.co_qualname.replace(".<locals>.", ".") == name and code.co_firstlineno <= owner.lineno]
        code = max(candidates, key=lambda item: item.co_firstlineno) if candidates else None
        graph.instructions = list(dis.get_instructions(code)) if code is not None else []
        pos = graph.pos(owner)
        end = graph.node("exit", pos, "return / end", "exit")
        error = graph.node("exception_exit", pos, "propagate exception", "exception")
        targets = {"return": end, "raise": error}
        body = graph.sequence(owner.body, end, targets, "")
        # A declaration's own summary labels its activity; never invented prose.
        documentation = ast.get_docstring(owner, clean=True)
        label = documentation.split("\n\n", 1)[0].replace("\n", " ") if documentation else name
        entry = graph.node("entry", pos, label, "entry")
        graph.edge(entry, body)
        facts.extend(graph.facts())
    from rey_lib.repository_map.command_bindings import python_command_bindings, python_request_bindings
    bindings: dict[tuple[str, int, int, str], set[str]] = {}
    for binding in (python_command_bindings(path, language, source_path)
                    + python_request_bindings(path, language, source_path)):
        row = binding.fields
        key = (row["owner_qualified_name"], row["owner_line"], row["owner_column"], row["node_key"])
        bindings.setdefault(key, set()).add(row["command"])
    for fact in facts:
        row = fact.fields
        if row["record_type"] == "control_flow_node":
            row["commands"] = sorted(bindings.get((row["owner_qualified_name"], row["owner_line"],
                                                   row["owner_column"], row["node_key"]), set()))
    return facts


@dataclass(frozen=True)
class _Syntax:
    """Plain values copied from the existing parser, not a second parser/tree owner.

    Native Tree-sitter Node wrappers are not retained across recursive CFG
    construction. This also keeps grammar ownership entirely in js_extractor.
    """
    type: str
    start_point: tuple[int, int]
    end_point: tuple[int, int]
    text: bytes
    named_children: tuple[Any, ...]
    fields: dict[str, Any]
    call_identity: tuple[str, str] | None

    def child_by_field_name(self, field: str) -> Any:
        return self.fields.get(field)


def _syntax_values(root: Any) -> _Syntax:
    from rey_lib.repository_map.js_extractor import _classify_call
    result = {}
    stack = [(root, False)]
    while stack:
        node, ready = stack.pop()
        children = node.children
        if not ready:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(children))
            continue
        fields = {node.field_name_for_child(i): result[child.id]
                  for i, child in enumerate(children) if node.field_name_for_child(i)}
        start = node.start_point
        end = node.end_point
        result[node.id] = _Syntax(node.type, (start[0], start[1]), (end[0], end[1]),
            node.text, tuple(result[c.id] for c in children if c.is_named), fields,
            _classify_call(node) if node.type in ("call_expression", "new_expression") else None)
    return result[root.id]


class JavaScriptFlow(FlowGraph):
    """Static statement flow using the existing Tree-sitter JavaScript family."""
    def pos(self, node: Any) -> tuple[int, int, int, int]:
        return (node.start_point[0] + 1, node.start_point[1],
                node.end_point[0] + 1, node.end_point[1])

    def make(self, node: Any, kind: str, ctx: str, text: str | None = None) -> str:
        from rey_lib.repository_map.js_extractor import js_text
        return self.node(kind, self.pos(node), js_text(node) if text is None else text, ctx)

    def expression(self, node: Any, after: str, targets: dict[str, str], ctx: str) -> str:
        if node is None:
            return after
        kind = node.type
        if kind in ("optional_chain", "type_assertion", "as_expression", "satisfies_expression"):
            self.reasons.add(kind)
        if kind in ("arrow_function", "function_expression", "generator_function"):
            return after  # defining a function does not execute its body
        if kind in ("ternary_expression", "conditional_expression"):
            yes = self.expression(node.child_by_field_name("consequence"), after, targets, ctx)
            no = self.expression(node.child_by_field_name("alternative"), after, targets, ctx)
            return self.condition(node.child_by_field_name("condition"), yes, no, targets, ctx)
        if kind == "binary_expression":
            from rey_lib.repository_map.js_extractor import js_text
            operator = node.child_by_field_name("operator")
            op = js_text(operator) if operator else ""
            if op in ("&&", "||"):
                right = self.expression(node.child_by_field_name("right"), after, targets, ctx)
                return self.condition(node.child_by_field_name("left"),
                                      right if op == "&&" else after,
                                      after if op == "&&" else right, targets, ctx)
            if op == "??":
                self.reasons.add("nullish short-circuit")
                key = self.make(node, "boundary", ctx)
                self.edge(key, after, "unknown")
                return key
        if kind in ("call_expression", "new_expression"):
            classified = node.call_identity
            callee, edge_kind = classified if classified else (None, None)
            key = self.node("call", self.pos(node), node.text.decode(), ctx, callee, edge_kind)
            self.edge(key, after)
            self.edge(key, targets["raise"], "exception")
            result = key
            for child in reversed(node.named_children):
                result = self.expression(child, result, targets, ctx)
            return result
        if kind in ("await_expression", "yield_expression"):
            self.reasons.add(kind + " suspension/cancellation")
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "resume")
            self.edge(key, targets["raise"], "exception")
            result = key
        else:
            result = after
        for child in reversed(node.named_children):
            result = self.expression(child, result, targets, ctx)
        return result

    def condition(self, node: Any, yes: str, no: str, targets: dict[str, str], ctx: str) -> str:
        branch = self.make(node, "condition", ctx)
        self.edge(branch, yes, "true")
        self.edge(branch, no, "false")
        self.edge(branch, targets["raise"], "exception")
        return self.expression(node, branch, targets, ctx)

    def sequence(self, nodes: list[Any], after: str, targets: dict[str, str], ctx: str) -> str:
        result = after
        for node in reversed(nodes):
            result = self.statement(node, result, targets, ctx)
        return result

    def statement(self, node: Any, after: str, targets: dict[str, str], ctx: str) -> str:
        kind = node.type
        field = node.child_by_field_name
        if kind == "statement_block":
            return self.sequence(node.named_children, after, targets, ctx)
        if kind == "else_clause":
            return self.sequence(node.named_children, after, targets, ctx)
        if kind == "if_statement":
            yes = self.statement(field("consequence"), after, targets, ctx)
            alternative = field("alternative")
            no = self.statement(alternative, after, targets, ctx) if alternative else after
            return self.condition(field("condition"), yes, no, targets, ctx)
        if kind in ("while_statement", "do_statement", "for_statement"):
            loop = self.make(node, "loop", ctx, kind)
            increment = field("increment")
            repeat = self.expression(increment, loop, targets, ctx)
            body = self.statement(field("body"), repeat,
                                  {**targets, "break": after, "continue": repeat}, ctx)
            condition = field("condition")
            test = self.condition(condition, body, after, targets, ctx) if condition else body
            self.edge(loop, test, "repeat")
            start = body if kind == "do_statement" else loop
            initializer = field("initializer")
            return self.expression(initializer, start, targets, ctx)
        if kind == "try_statement":
            finalizer = field("finalizer")
            routed = dict(targets)
            normal = after
            if finalizer:
                body = finalizer.child_by_field_name("body") or finalizer.named_children[-1]
                routed = {completion: self.statement(body, dest, targets,
                          f"{ctx}/finally@{node.start_point[0]}:{completion}")
                          for completion, dest in targets.items()}
                normal = self.statement(body, after, targets,
                                        f"{ctx}/finally@{node.start_point[0]}:normal")
            handler = field("handler")
            dispatch = self.statement(handler.child_by_field_name("body"), normal, routed, ctx) if handler else routed["raise"]
            return self.statement(field("body"), normal, {**routed, "raise": dispatch}, ctx)
        abrupt = {"return_statement": "return", "throw_statement": "raise",
                  "break_statement": "break", "continue_statement": "continue"}
        if kind in abrupt:
            completion = abrupt[kind]
            # Labelled transfers need a lexical label target map, not guessing.
            if kind in ("break_statement", "continue_statement") and node.named_children:
                self.reasons.add("labelled transfer")
                key = self.make(node, "boundary", ctx)
                self.edge(key, targets["raise"], "unknown")
                return key
            key = self.make(node, completion, ctx)
            self.edge(key, targets[completion], completion)
            result = key
            for child in reversed(node.named_children):
                result = self.expression(child, result, targets, ctx)
            return result
        if kind in ("function_declaration", "generator_function_declaration", "class_declaration"):
            key = self.make(node, "definition", ctx)
            self.edge(key, after)
            if kind == "class_declaration":
                self.reasons.add("class initialization")
            return key
        if kind not in ("expression_statement", "lexical_declaration", "variable_declaration",
                        "empty_statement", "comment"):
            self.reasons.add(kind)
            key = self.make(node, "boundary", ctx)
            self.edge(key, after, "unknown")
            self.edge(key, targets["raise"], "exception")
            return key
        key = self.make(node, "statement", ctx)
        self.edge(key, after)
        self.edge(key, targets["raise"], "exception")
        return self.expression(node, key, targets, ctx)


def javascript_control_flow(path: Any, language: str, source_path: str | None = None) -> list[FlowFact]:
    """Use the same owners, positions and parser as JS return/argument extraction."""
    from rey_lib.repository_map.js_extractor import _parse, _js_recorded_bodies, _js_owner_position
    facts = []
    root = _parse(path, language)
    for owner, name in _js_recorded_bodies(root):
        line, column = _js_owner_position(owner)
        graph = JavaScriptFlow(source_path or path.as_posix(), name, line, column)
        syntax = _syntax_values(owner)
        end = graph.node("exit", graph.pos(syntax), "return / end", "exit")
        error = graph.node("exception_exit", graph.pos(syntax), "propagate exception", "exception")
        body = syntax.child_by_field_name("body")
        if body is None:
            graph.reasons.add("missing declaration body")
            entry = graph.make(syntax, "boundary", "entry")
            graph.edge(entry, end, "unknown")
        else:
            start = graph.statement(body, end, {"return": end, "raise": error}, "")
            entry = graph.make(syntax, "entry", "entry", name)
            graph.edge(entry, start)
        facts.extend(graph.facts())
    return facts
