from types import SimpleNamespace
from rey_lib.config.applications import Application
from rey_lib.inferred_architecture.object_map import ObjectMap
from rey_lib.db import connection


def test_application_retains_its_own_canonical_object_map(monkeypatch):
    calls = []
    controls = []
    monkeypatch.setattr(connection, 'shared_connection', lambda ctx, name: (ctx, name))
    def build(conn, application):
        calls.append((conn, application))
        return SimpleNamespace(projection=lambda:{'subject':{'application':application}},
                               bind_layout=controls.append)
    monkeypatch.setattr(ObjectMap, 'for_application', staticmethod(build))
    ctx = SimpleNamespace(shared_control=object())
    first, second = Application(name='first'), Application(name='second')
    assert 'map' not in first.projection()
    assert first.application_map(ctx) is first.application_map(ctx)
    assert first.application_map(ctx) is not second.application_map(ctx)
    assert [app for _, app in calls] == ['first', 'second']
    assert controls == [ctx.shared_control, ctx.shared_control]
    assert first.projection()['map']['subject']['application'] == 'first'
    assert second.projection()['map']['subject']['application'] == 'second'
