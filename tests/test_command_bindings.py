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


_HOSTED = '''from rey_lib.run.hosted_objects import HostedObjects
class Root:
    def answer(self, command):
        name = str(command.get('command') or '')
        if name == 'select':
            select()
        if name != 'task':
            raise ValueError(name)
        task()
class Task:
    def answer(self, command):
        if command.get('command') == 'run':
            execute()
class Unregistered:
    def answer(self, command):
        if command.get('command') == 'invented':
            arbitrary()
def hosted_objects():
    def construct(subject, identity, emit):
        return Root()
    def hold_task():
        return hosted.hold(lambda emit: Task())
    hosted = HostedObjects(construct)
    return hosted
'''


def test_registered_hosted_factories_prove_only_their_objects_request_guards(tmp_path):
    rows = commands(tmp_path, _HOSTED)
    assert {(r['owner_qualified_name'], tuple(r['commands'])) for r in rows} == {
        ('Root.answer', ('request:select',)), ('Root.answer', ('request-false:task',)),
        ('Task.answer', ('request:run',))}


def test_arbitrary_owner_with_same_spelling_does_not_prove_a_hosted_handler(tmp_path):
    assert commands(tmp_path, _HOSTED.replace(
        'from rey_lib.run.hosted_objects import HostedObjects', 'from unrelated import HostedObjects')) == []


def test_shadowed_host_constructor_and_mutated_message_establish_no_request(tmp_path):
    assert commands(tmp_path, _HOSTED.replace('def hosted_objects():', 'def hosted_objects(HostedObjects):')) == []
    source = _HOSTED.replace("        if command.get('command') == 'run':",
                            "        command['command'] = arbitrary()\n        if command.get('command') == 'run':")
    assert all(r['owner_qualified_name'] != 'Task.answer' for r in commands(tmp_path, source))


def test_request_provenance_follows_local_protocol_dispatch(tmp_path):
    rows = commands(tmp_path, '''class Host:
    def serve(self, channel):
        for command in channel.commands():
            name = str(command.get('command') or '')
            if name == 'close':
                break
            self._answer(name, command)
    def _answer(self, name, command):
        if name == 'open':
            open_object()
        elif command.get('command') in ('projection', 'release'):
            read_or_release()
    def unrelated(self, command):
        if command.get('command') == 'unknown':
            unknown()
''')
    assert {(r['owner_qualified_name'], tuple(r['commands'])) for r in rows} == {
        ('Host.serve', ('request:close',)), ('Host._answer', ('request:open',)),
        ('Host._answer', ('request:projection', 'request:release'))}


def test_imported_str_does_not_prove_a_command_selector(tmp_path):
    source = _HOSTED.replace('class Root:', 'from unknown import str\nclass Root:')
    assert all(r['owner_qualified_name'] != 'Root.answer' for r in commands(tmp_path, source))


def test_alias_and_module_imports_preserve_the_canonical_host_identity(tmp_path):
    for source in [
        _HOSTED.replace('import HostedObjects', 'import HostedObjects as Objects').replace('= HostedObjects(', '= Objects('),
        _HOSTED.replace('from rey_lib.run.hosted_objects import HostedObjects', 'import rey_lib.run.hosted_objects').replace('= HostedObjects(', '= rey_lib.run.hosted_objects.HostedObjects('),
    ]:
        assert {r['owner_qualified_name'] for r in commands(tmp_path, source)} == {'Root.answer', 'Task.answer'}


def test_a_loop_selector_does_not_turn_post_loop_work_into_a_request_handler(tmp_path):
    assert commands(tmp_path, '''def serve(channel):
    for message in channel.commands():
        name = message.get('command')
    if name == 'run':
        after_channel_closed()
''') == []
