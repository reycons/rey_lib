"""Page-owned hosts share a process, never their objects' state or lifetime."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

from rey_lib.run.app_transports import AppTransports
from rey_lib.run.hosted_objects import HostedObjects, ServedObject


def test_concurrent_openings_and_different_operations_retain_one_application(monkeypatch):
    owner = AppTransports()
    launches = []

    def launch(runtime, application, operation, inputs, listener):
        transport = SimpleNamespace(application=application, ended=lambda: False,
                                    connected=lambda **_: True)
        with owner._guard:
            ref = str(len(launches))
            launches.append((application, operation))
            owner._held[ref] = transport
        return ref, transport, lambda: None

    monkeypatch.setattr(owner, "launch", launch)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda op: owner.application(None, "data_engine", op),
                                   ["serve", "serve_load"] * 12))
    assert len(launches) == 1
    assert len({ref for ref, _ in results}) == 1


def test_serving_loops_are_isolated_and_close_one_preserves_the_other():
    ended = []
    changed = threading.Event()

    def construct(subject, identity, emit):
        def serve(channel):
            value = identity["value"]
            channel.emit({"projection": {"value": value}})
            for command in channel.commands():
                if command["command"] == "close":
                    break
                value = command["value"]
                channel.emit({"projection": {"value": value}, "request_id": command["request_id"]})
                changed.set()
            ended.append(identity["value"])
        return ServedObject(serve, emit)

    held = HostedObjects(construct)
    emitted = []
    a, _ = held.open("", {"value": "A"}, emitted.append)
    b, _ = held.open("", {"value": "B"}, emitted.append)
    try:
        held._holding(a).answer({"command": "edit", "object": a, "value": "edited", "request_id": "r"})
        assert changed.wait(2)
        assert held.projection(a) == {"value": "edited"}
        assert held.projection(b) == {"value": "B"}
        held.release(a)
        assert held.projection(b) == {"value": "B"}
        assert len(held) == 1
    finally:
        class Channel:
            def commands(self): return [{"command": "close"}]
            def emit(self, message): pass
        held.serve(Channel())
    assert sorted(ended) == ["A", "B"]


def test_real_process_is_reused_within_a_page_and_isolated_between_pages(monkeypatch):
    import sys
    from rey_lib.run.app_channel import LaunchedApp
    program = r"""
import os
from rey_lib.run.app_channel import AttachedApp
from rey_lib.run.hosted_objects import HostedObjects
class Held:
    def __init__(self, subject, identity, emit): self.identity = identity
    def projection(self): return {"pid": os.getpid(), **self.identity}
    def answer(self, command): pass
    def release(self): pass
channel = AttachedApp.attach(os.getpid())
try: HostedObjects(Held).serve(channel)
finally: channel.close()
"""
    launches = []
    def launch(runtime, application, operation, inputs):
        assert inputs == {}
        launches.append((application, operation))
        return LaunchedApp.launch([sys.executable, "-c", program])
    monkeypatch.setattr(LaunchedApp, "launch_operation", launch)
    first, second = AppTransports(), AppTransports()
    try:
        ref, host = first.application(None, "example", "serve")
        other_ref, same = first.application(None, "example", "other_hosted_operation")
        assert (ref, host) == (other_ref, same)
        a = host.request({"command": "open", "identity": {"name": "A"}})
        b = host.request({"command": "open", "identity": {"name": "B"}})
        assert a["object"] != b["object"]
        assert a["projection"]["pid"] == b["projection"]["pid"]
        host.request({"command": "release", "object": a["object"]})
        assert host.request({"command": "projection", "object": b["object"]})["projection"]["name"] == "B"
        _, other = second.application(None, "example", "serve")
        c = other.request({"command": "open", "identity": {"name": "C"}})
        assert c["projection"]["pid"] != b["projection"]["pid"]
        first.close_all()
        assert host.ended()
        assert other.request({"command": "projection", "object": c["object"]})["projection"]["name"] == "C"
        assert launches == [("example", "serve"), ("example", "serve")]
    finally:
        first.close_all()
        second.close_all()


def test_released_run_observer_does_not_retain_a_closed_object():
    from rey_lib.run import AppExecute
    class Manifest:
        def create_run_manifest(self, **kw): return 1
    run = AppExecute.start(Manifest(), subject_type="app", subject_id="host")
    heard = []
    release = run.observe(lambda child: heard.append(child))
    run.write("before")
    release()
    release()
    run.write("after")
    assert heard == [run]


def test_a_serving_object_that_fails_is_removed_without_ending_its_neighbors():
    import queue
    from rey_lib.errors.error_utils import AppError
    events = queue.Queue()
    def construct(subject, identity, emit):
        def serve(channel):
            channel.emit({"projection": identity})
            for command in channel.commands():
                if command["command"] == "close": return
                raise AppError("The object's resource failed")
        return ServedObject(serve, emit)
    objects = HostedObjects(construct)
    a, _ = objects.open("", {"name": "A"}, events.put)
    b, _ = objects.open("", {"name": "B"}, events.put)
    try:
        objects._holding(a).answer({"command": "fail"})
        assert events.get(timeout=2)["error"] == "The object's resource failed"
        assert events.get(timeout=2) == {"object": a, "released": True}
        assert len(objects) == 1
        assert objects.projection(b) == {"name": "B"}
    finally:
        class Channel:
            def commands(self): return [{"command": "close"}]
            def emit(self, message): pass
        objects.serve(Channel())


def test_failed_startup_preserves_the_application_error_and_leaves_no_host(monkeypatch):
    import sys
    import pytest
    from rey_lib.run.app_channel import LaunchedApp
    from rey_lib.errors.error_utils import AppError
    monkeypatch.setattr(LaunchedApp, "launch_operation", lambda *args:
        LaunchedApp.launch([sys.executable, "-c", "import sys; print('ConfigError: required setting missing', file=sys.stderr); sys.exit(1)"]))
    owner = AppTransports()
    with pytest.raises(AppError, match="required setting missing"):
        owner.application(None, "missing", "serve")
    assert owner.held() == []


def test_unexpected_initial_hydration_failure_is_an_open_refusal_not_a_host_failure(monkeypatch):
    from rey_lib.errors.error_utils import AppError
    reported = []
    monkeypatch.setattr(AppError, "report", lambda self, *args, **kw: reported.append(str(self)))
    def construct(subject, identity, emit):
        if identity.get("broken"):
            raise ValueError("Malformed hydrated record")
        def serve(channel):
            channel.emit({"projection": {"name": "good"}})
            for command in channel.commands():
                if command["command"] == "close": return
        return ServedObject(serve, emit)
    class Channel:
        emitted = []
        def commands(self): return [
            {"command": "open", "identity": {"broken": True}, "request_id": "bad"},
            {"command": "open", "identity": {}, "request_id": "good"},
            {"command": "close"}]
        def emit(self, message): self.emitted.append(message)
    channel = Channel()
    HostedObjects(construct).serve(channel)
    assert channel.emitted[0]["error"] == "Malformed hydrated record"
    assert channel.emitted[1]["projection"] == {"name": "good"}
    assert reported == ["Malformed hydrated record"]
