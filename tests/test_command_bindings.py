"""Command bindings are syntax evidence, never a guessed handler vocabulary."""
from rey_lib.repository_map.control_flow import python_control_flow


def commands(tmp_path, source):
    path = tmp_path / 'main.py'
    path.write_text(source)
    return [r.to_dict() for r in python_control_flow(path, 'Python', path.name)
            if r.fields['record_type'] == 'control_flow_node' and r.fields['commands']]


def test_parser_namespace_forwarding_including_captured_callback(tmp_path):
    rows = commands(tmp_path, '''import argparse
def parse():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=('serve', 'load'))
    args = p.parse_args()
    return args
def main():
    args = parse()
    selected = str(args.command)
    if selected == 'serve':
        serve()
    consume(lambda: dispatch(args))
def dispatch(args):
    if args.command == 'load':
        load()
''')
    assert {(r['owner_qualified_name'], tuple(r['commands'])) for r in rows} == {
        ('main', ('serve',)), ('dispatch', ('load',))}
    assert all(r['kind'] == 'condition' for r in rows)


def test_subparser_destination_is_proved_but_unrelated_named_field_is_not(tmp_path):
    rows = commands(tmp_path, '''from argparse import ArgumentParser
def main():
    parser = ArgumentParser()
    parser.add_subparsers(dest='operation')
    args = parser.parse_args()
    if args.operation == 'serve':
        serve()
def unrelated(args):
    if args.operation == 'serve':
        unrelated_serve()
''')
    assert [(r['owner_qualified_name'], r['commands']) for r in rows] == [('main', ['serve'])]


def test_reassigned_selector_and_shadowed_callback_do_not_establish_bindings(tmp_path):
    assert commands(tmp_path, '''import argparse
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('serve',))
    args = parser.parse_args()
    args = other()
    if args.command == 'serve':
        serve()
    consume(lambda args: dispatch(args))
def dispatch(args):
    if args.command == 'serve':
        serve()
''') == []


def test_plain_strings_and_dynamic_choices_do_not_establish_command_bindings(tmp_path):
    assert commands(tmp_path, '''import argparse
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=choices())
    args = parser.parse_args()
    if args.command == 'serve':
        serve()
''') == []


def test_shadowed_parser_and_local_function_names_are_not_resolved_by_spelling(tmp_path):
    assert commands(tmp_path, """import argparse
def parse(argparse):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('serve',))
    return parser.parse_args()
def main(parse):
    args = parse()
    if args.command == 'serve':
        serve()
""") == []


def test_a_callback_parameter_cannot_be_the_captured_parser_namespace(tmp_path):
    assert commands(tmp_path, """import argparse
def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=('serve',))
    args = p.parse_args()
    consume(lambda args: dispatch(args))
def dispatch(args):
    if args.command == 'serve':
        serve()
""") == []


def test_literal_cli_dispatch_binds_each_input_to_its_handler(tmp_path):
    rows = commands(tmp_path, '''import argparse
def parse():
    p = argparse.ArgumentParser()
    p.add_subparsers(dest='command')
    return p.parse_args()
def main():
    args = parse()
    handlers = {'scan': scan, 'index': index}
    handler = handlers.get(args.command)
    if handler is None:
        raise ValueError(args.command)
    return handler(args)
def scan(args):
    report()
def index(args):
    publish()
''')
    assert {(r['owner_qualified_name'], r['kind'], tuple(r['commands'])) for r in rows} == {
        ('scan', 'entry', ('scan',)), ('index', 'entry', ('index',))}


def test_mutated_dispatch_table_does_not_prove_a_handler(tmp_path):
    assert commands(tmp_path, '''import argparse
def main():
    p = argparse.ArgumentParser()
    p.add_subparsers(dest='command')
    args = p.parse_args()
    handlers = {'scan': scan}
    handlers.update(dynamic())
    handler = handlers.get(args.command)
    return handler(args)
def scan(args):
    pass
''') == []


def test_channel_requests_are_separate_from_cli_and_ordinary_fields(tmp_path):
    rows = commands(tmp_path, '''def serve(channel, obj):
    for message in channel.commands():
        name = str(message.get('command') or '')
        if name == 'run':
            obj.run()
        elif name in ('projection', 'inspect'):
            obj.inspect()
def unrelated(message):
    name = message.get('command')
    if name == 'run':
        other()
''')
    assert {tuple(r['commands']) for r in rows} == {
        ('request:run',), ('request:inspect', 'request:projection')}
    assert all(r['owner_qualified_name'] == 'serve' and r['kind'] == 'condition' for r in rows)


def test_reassigned_request_selector_is_not_a_binding(tmp_path):
    assert commands(tmp_path, '''def serve(channel):
    for message in channel.commands():
        name = message.get('command')
        name = arbitrary()
        if name == 'run':
            run()
''') == []
