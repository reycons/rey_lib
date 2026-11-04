"""CLI-command guard facts from the existing Python parser, not handler guesses.

Recognizes namespaces produced by a locally constructed argparse.ArgumentParser,
local functions returning that namespace, and explicit forwarding to local
parameters. It never imports or executes scanned modules. Unsupported/dynamic
routing simply establishes no binding.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from rey_lib.repository_map.control_flow import FlowFact

__all__ = ["python_command_bindings", "python_request_bindings"]


def python_command_bindings(path: Path, language: str, source_path: str | None = None) -> list[FlowFact]:
    from rey_lib.repository_map.python_extractor import _parse, _own_nodes
    tree = _parse(path)
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    module_writes = {n.id for statement in tree.body
                     if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                     for n in ast.walk(statement) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    imports = {key: value for key, value in imports.items() if key not in module_writes}
    declarations = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    counts = Counter(n.name for n in declarations)
    functions = {n.name: n for n in declarations if counts[n.name] == 1}
    bodies = {name: list(_own_nodes(node)) for name, node in functions.items()}
    assignments = {name: [n for n in bodies[name] if isinstance(n, ast.Assign) and len(n.targets) == 1
                          and isinstance(n.targets[0], ast.Name)] for name in functions}
    writes = {name: Counter(n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
              for name, nodes in bodies.items()}
    parameters_by_function = {
        name: {p.arg for p in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
              | {p.arg for p in (fn.args.vararg, fn.args.kwarg) if p}
        for name, fn in functions.items()}
    local_names = {name: set(writes[name]) | parameters_by_function[name] for name in functions}
    namespaces: dict[str, dict[str, set[str]]] = {name: {} for name in functions}
    returns: dict[str, set[str]] = {name: set() for name in functions}
    parsers = {name: {} for name in functions}
    for name, nodes in bodies.items():
        for assignment in assignments[name]:
            value = assignment.value
            if isinstance(value, ast.Call):
                func = value.func
                qualified = (imports.get(func.id) if isinstance(func, ast.Name) else
                             f"{imports.get(func.value.id)}.{func.attr}"
                             if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None)
                target = assignment.targets[0].id
                root = func.id if isinstance(func, ast.Name) else (
                    func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None)
                if (qualified == "argparse.ArgumentParser" and root not in local_names[name]
                    and writes[name][target] == 1):
                    parsers[name][target] = set()
        for node in nodes:
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.attr == "add_subparsers"
                and node.func.value.id in parsers[name]):
                for keyword in node.keywords:
                    if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        parsers[name][node.func.value.id].add(keyword.value.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.attr == "add_argument"
                and node.func.value.id in parsers[name] and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and not node.args[0].value.startswith("-")):
                # Positional choices are also explicit command selectors; plain
                # strings/options are not evidence of a command declaration.
                if any(k.arg == "choices" and isinstance(k.value, (ast.Tuple, ast.List))
                       and all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                               for v in k.value.elts) for k in node.keywords):
                    destination = next((k.value.value for k in node.keywords
                                        if k.arg == "dest" and isinstance(k.value, ast.Constant)
                                        and isinstance(k.value.value, str)), node.args[0].value)
                    parsers[name][node.func.value.id].add(destination)

    def calls(nodes):
        for node in nodes:
            if isinstance(node, ast.Call):
                yield node, set()
            elif isinstance(node, ast.Lambda):
                # A closure may forward the same namespace. Parameters shadow
                # enclosing names; nested closures are left unbound.
                shadowed = {p.arg for p in (*node.args.posonlyargs, *node.args.args,
                                           *node.args.kwonlyargs)}
                shadowed.update(p.arg for p in (node.args.vararg, node.args.kwarg) if p)
                for child in _own_nodes(node):
                    if isinstance(child, ast.Call):
                        yield child, shadowed

    def namespace(name: str, value: ast.AST | None) -> set[str]:
        if isinstance(value, ast.Name):
            return namespaces[name].get(value.id, set())
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and value.func.id in functions and value.func.id not in local_names[name]:
                return returns[value.func.id]
            func = value.func
            if isinstance(func, ast.Attribute) and func.attr == "parse_args" and isinstance(func.value, ast.Name):
                return parsers[name].get(func.value.id, set())
        return set()

    # Finite propagation of source facts, never evaluating values or resolving
    # arbitrary receivers. Multiple proven callers can add destinations only.
    changed = True
    while changed:
        changed = False
        for name, fn in functions.items():
            for assignment in assignments[name]:
                target = assignment.targets[0].id
                if writes[name][target] != 1:
                    continue
                known = namespace(name, assignment.value)
                if known - namespaces[name].get(target, set()):
                    namespaces[name].setdefault(target, set()).update(known)
                    changed = True
            for node in bodies[name]:
                if isinstance(node, ast.Return):
                    known = namespace(name, node.value)
                    if known - returns[name]:
                        returns[name].update(known)
                        changed = True
            for node, shadowed in calls(bodies[name]):
                if not isinstance(node.func, ast.Name):
                    continue
                callee = node.func.id
                if (callee not in functions or callee in local_names[name] or callee in shadowed
                    or any(isinstance(arg, ast.Starred) for arg in node.args)
                    or any(k.arg is None for k in node.keywords)):
                    continue
                parameters = [*functions[callee].args.posonlyargs, *functions[callee].args.args]
                supplied = [(p.arg, arg) for p, arg in zip(parameters, node.args)]
                supplied += [(k.arg, k.value) for k in node.keywords if k.arg]
                for param, arg in supplied:
                    if param not in parameters_by_function[callee] or writes[callee][param]:
                        continue
                    known = (set() if isinstance(arg, ast.Name) and arg.id in shadowed
                             else namespace(name, arg))
                    if known - namespaces[callee].get(param, set()):
                        namespaces[callee].setdefault(param, set()).update(known)
                        changed = True

    def conditions(statements):
        for node in statements:
            if isinstance(node, ast.If):
                yield node.test
                yield from conditions(node.body)
                yield from conditions(node.orelse)
            elif isinstance(node, (ast.For, ast.While, ast.With, ast.Try)):
                yield from conditions(node.body)
                yield from conditions(getattr(node, "orelse", []))
                for handler in getattr(node, "handlers", []):
                    yield from conditions(handler.body)
                # Finally copies have completion-specific identities; they are
                # cleanup, not an entry into an application's command operation.

    result = []
    for name, fn in functions.items():
        aliases = {}
        for assignment in assignments[name]:
            target = assignment.targets[0].id
            value = assignment.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "str" and len(value.args) == 1
                and "str" not in imports and "str" not in module_writes
                and "str" not in local_names[name]):
                value = value.args[0]
            if (writes[name][target] == 1 and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name) and value.attr in namespaces[name].get(value.value.id, set())):
                aliases[target] = assignment.lineno
        for test in conditions(fn.body):
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            left, right = test.left, test.comparators[0]
            known = (isinstance(left, ast.Attribute) and isinstance(left.value, ast.Name)
                     and left.attr in namespaces[name].get(left.value.id, set()))
            known = known or (isinstance(left, ast.Name) and left.id in aliases and aliases[left.id] < test.lineno)
            if not known:
                continue
            values = ([right.value] if isinstance(test.ops[0], ast.Eq) and isinstance(right, ast.Constant)
                      else [v.value for v in right.elts if isinstance(v, ast.Constant)]
                      if isinstance(test.ops[0], ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)) else [])
            for value in values:
                if not isinstance(value, str):
                    continue
                key = f"{test.lineno}:{test.col_offset}:{test.end_lineno}:{test.end_col_offset}:condition:"
                result.append(FlowFact({"record_type": "command_binding",
                    "record_id": f"command_binding:{source_path or path.as_posix()}:{name}:{key}:{value}",
                    "source_path": source_path or path.as_posix(), "owner_qualified_name": name,
                    "owner_line": fn.lineno, "owner_column": fn.col_offset,
                    "node_key": key, "command": value, "branch": "true"}))
    # A literal dispatch table selected by a proved argparse namespace binds
    # directly to the local handler's entry, rather than the whole main().
    for name, fn in functions.items():
        maps = {}
        for assignment in assignments[name]:
            target, value = assignment.targets[0].id, assignment.value
            if writes[name][target] != 1 or not isinstance(value, ast.Dict):
                continue
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                       and isinstance(v, ast.Name) and v.id in functions
                       and v.id not in local_names[name] for k, v in zip(value.keys, value.values)):
                continue
            if len({k.value for k in value.keys}) != len(value.keys):
                continue
            # Escaping/mutating the table invalidates this small static proof.
            uses = [n for n in bodies[name] if isinstance(n, ast.Name) and n.id == target
                    and isinstance(n.ctx, ast.Load)]
            lookups = [n for n in bodies[name] if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute) and n.func.attr == 'get'
                       and isinstance(n.func.value, ast.Name) and n.func.value.id == target
                       and len(n.args) == 1 and not n.keywords]
            if len(uses) != len(lookups):
                continue
            maps[target] = (assignment, dict(zip((k.value for k in value.keys),
                                               (v.id for v in value.values))))
        for assignment in assignments[name]:
            lookup = assignment.value
            if not (isinstance(lookup, ast.Call) and isinstance(lookup.func, ast.Attribute)
                    and isinstance(lookup.func.value, ast.Name)
                    and lookup.func.value.id in maps and lookup.func.attr == 'get'
                    and len(lookup.args) == 1 and not lookup.keywords):
                continue
            selector = lookup.args[0]
            if not (isinstance(selector, ast.Attribute) and isinstance(selector.value, ast.Name)
                    and selector.attr in namespaces[name].get(selector.value.id, set())):
                continue
            variable = assignment.targets[0].id
            if writes[name][variable] != 1 or not any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == variable and n.lineno > assignment.lineno for n in bodies[name]):
                continue
            declaration, entries = maps[lookup.func.value.id]
            if declaration.lineno >= assignment.lineno:
                continue
            for command, handler in entries.items():
                owner = functions[handler]
                key = f"{owner.lineno}:{owner.col_offset}:{owner.end_lineno}:{owner.end_col_offset}:entry:entry"
                result.append(FlowFact({"record_type": "command_binding",
                    "record_id": f"command_binding:{source_path or path.as_posix()}:{handler}:{key}:{command}",
                    "source_path": source_path or path.as_posix(), "owner_qualified_name": handler,
                    "owner_line": owner.lineno, "owner_column": owner.col_offset,
                    "node_key": key, "command": command, "branch": "entry"}))
    return result


def _hosted_answer_parameters(tree: ast.Module) -> dict[str, str]:
    """Prove local objects supplied to the canonical HostedObjects factories."""
    from rey_lib.repository_map.python_extractor import _own_nodes
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
        elif isinstance(node, ast.Import):
            imports.update({a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0] for a in node.names})
    canonical = 'rey_lib.run.hosted_objects.HostedObjects'
    if not any(value == canonical or canonical.startswith(value + '.') for value in imports.values()):
        return {}
    stores = Counter(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    parameters = {a.arg for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                  for a in (*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs)}
    def qualified(node):
        if isinstance(node, ast.Name) and not stores[node.id] and node.id not in parameters:
            return imports.get(node.id)
        if isinstance(node, ast.Attribute):
            base = qualified(node.value)
            return f"{base}.{node.attr}" if base else None
        return None
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef) and not stores[n.name]}
    offered = set()
    def factory(value):
        if isinstance(value, ast.Name) and len(functions.get(value.id, [])) == 1 and not stores[value.id] and value.id not in parameters:
            owner = functions[value.id][0]
            returned = [n.value for n in _own_nodes(owner) if isinstance(n, ast.Return)]
        elif isinstance(value, ast.Lambda):
            returned = [value.body]
        else:
            return
        for result in returned:
            if isinstance(result, ast.Call) and isinstance(result.func, ast.Name) and result.func.id in classes:
                offered.add(result.func.id)
    hosts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or qualified(node.func) != 'rey_lib.run.hosted_objects.HostedObjects':
            continue
        value = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'construct'), None)
        factory(value)
        for assignment in ast.walk(tree):
            if isinstance(assignment, ast.Assign) and assignment.value is node and len(assignment.targets) == 1:
                target = assignment.targets[0]
                if isinstance(target, ast.Name) and stores[target.id] == 1 and target.id not in parameters:
                    hosts.add(target.id)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'hold'
                and isinstance(node.func.value, ast.Name) and node.func.value.id in hosts):
            value = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'make'), None)
            factory(value)
    result = {}
    for name in offered:
        answers = [n for n in classes[name].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'answer']
        if len(answers) == 1:
            args = [*answers[0].args.posonlyargs, *answers[0].args.args]
            if len(args) == 2 and args[0].arg == 'self':
                result[f'{name}.answer'] = args[1].arg
    return result


def python_request_bindings(path: Path, language: str, source_path: str | None = None) -> list[FlowFact]:
    """Prove channel requests and canonical hosted-object command dispatch.

    Message/selector provenance follows local calls only; arbitrary dict fields
    and unregistered answer methods establish no request binding. Negative guards
    retain their false-branch evidence in the existing commands[] namespace.
    """
    from rey_lib.repository_map.python_extractor import _parse, _own_nodes, _recorded_bodies
    tree = _parse(path)
    recorded = _recorded_bodies(tree)
    counts = Counter(name for _, name in recorded)
    functions = {name: fn for fn, name in recorded if counts[name] == 1}
    bodies = {name: list(_own_nodes(fn)) for name, fn in functions.items()}
    writes = {name: Counter(n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
              for name, nodes in bodies.items()}
    params = {name: [a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]
              for name, fn in functions.items()}
    messages = {name: {} for name in functions}
    selectors = {name: {} for name in functions}
    def add(table, name, variable, span):
        if table is messages and any(
                isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.value, ast.Name) and node.value.id == variable
                or isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == variable
                and node.func.attr in ('update', 'clear', 'pop', 'popitem', 'setdefault', '__setitem__', '__delitem__')
                for node in bodies[name]):
            return False
        held = table[name].setdefault(variable, set())
        before = len(held); held.add(span)
        return len(held) != before
    for name, variable in _hosted_answer_parameters(tree).items():
        if name in functions and not writes[name][variable]:
            add(messages, name, variable, (functions[name].lineno, functions[name].end_lineno))
    for name, nodes in bodies.items():
        for loop in nodes:
            if (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)
                and writes[name][loop.target.id] == 1 and isinstance(loop.iter, ast.Call)
                and isinstance(loop.iter.func, ast.Attribute) and loop.iter.func.attr == 'commands'
                and isinstance(loop.iter.func.value, ast.Name) and loop.iter.func.value.id in params[name]
                and not writes[name][loop.iter.func.value.id] and not loop.iter.args and not loop.iter.keywords):
                add(messages, name, loop.target.id, (loop.lineno, loop.end_lineno))
    if not any(messages.values()):
        return []
    shadowed_str = any(isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name) == 'str' for a in n.names)
                       or isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == 'str'
                       or isinstance(n, ast.Name) and n.id == 'str' and isinstance(n.ctx, ast.Store)
                       for n in tree.body)
    def known(table, name, node):
        return isinstance(node, ast.Name) and any(a <= node.lineno <= b for a, b in table[name].get(node.id, ()))
    def selector(name, value):
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == 'str'
            and not shadowed_str and 'str' not in params[name] and not writes[name]['str']
            and len(value.args) == 1 and not value.keywords):
            value = value.args[0]
        if (isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2
            and isinstance(value.values[1], ast.Constant) and value.values[1].value == ''):
            value = value.values[0]
        if known(selectors, name, value):
            return True
        return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and known(messages, name, value.func.value) and value.func.attr == 'get'
                and len(value.args) in (1, 2) and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == 'command' and not value.keywords)
    changed = True
    while changed:
        changed = False
        for name, nodes in bodies.items():
            for node in nodes:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and writes[name][node.targets[0].id] == 1 and selector(name, node.value)):
                    ends = [end for value in ast.walk(node.value) if isinstance(value, ast.Name)
                            for table in (messages, selectors)
                            for start, end in table[name].get(value.id, ()) if start <= node.lineno <= end]
                    changed |= add(selectors, name, node.targets[0].id,
                                   (node.lineno + 1, min(ends, default=functions[name].end_lineno)))
                if not isinstance(node, ast.Call) or any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                    continue
                callee, bound = None, False
                if isinstance(node.func, ast.Name) and node.func.id in functions and not writes[name][node.func.id] and node.func.id not in params[name]:
                    callee = node.func.id
                elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                      and node.func.value.id == 'self' and '.' in name and params[name][:1] == ['self']
                      and not writes[name]['self']):
                    candidate = name.rsplit('.', 1)[0] + '.' + node.func.attr
                    if candidate in functions:
                        callee, bound = candidate, True
                if not callee:
                    continue
                supplied = list(zip(params[callee][int(bound):], node.args)) + [(k.arg, k.value) for k in node.keywords]
                for variable, value in supplied:
                    if variable not in params[callee] or writes[callee][variable]:
                        continue
                    span = (functions[callee].lineno, functions[callee].end_lineno)
                    if known(messages, name, value):
                        changed |= add(messages, callee, variable, span)
                    if selector(name, value):
                        changed |= add(selectors, callee, variable, span)
    result = []
    for name, nodes in bodies.items():
        fn = functions[name]
        for node in nodes:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not selector(name, test.left):
                continue
            right = test.comparators[0]
            values = ([right] if isinstance(test.ops[0], (ast.Eq, ast.NotEq)) else right.elts
                      if isinstance(test.ops[0], (ast.In, ast.NotIn)) and isinstance(right, (ast.Tuple, ast.List, ast.Set)) else [])
            negative = isinstance(test.ops[0], (ast.NotEq, ast.NotIn))
            for value in values:
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                command = ('request-false:' if negative else 'request:') + value.value
                key = f'{test.lineno}:{test.col_offset}:{test.end_lineno}:{test.end_col_offset}:condition:'
                result.append(FlowFact({'record_type': 'command_binding',
                    'record_id': f'command_binding:{source_path or path.as_posix()}:{name}:{key}:{command}',
                    'source_path': source_path or path.as_posix(), 'owner_qualified_name': name,
                    'owner_line': fn.lineno, 'owner_column': fn.col_offset,
                    'node_key': key, 'command': command, 'branch': 'false' if negative else 'true'}))
    return result
