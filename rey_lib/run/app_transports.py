"""AppTransports: the common fabric through which Rey applications communicate (#1141).

An execution context -- the Console's PageRuntime is one, a scheduler or other
runtime may be another -- holds one AppTransports. It launches applications
from their semantic inputs, holds each one's AppTransport under an opaque ref,
carries commands to them and what they emit back, correlates requests with
their answers, and ends them with the context (rule
application_communication_is_common_infrastructure_only):

    execution context <-> AppTransports <-> AppTransport (by ref) <-> application

The pre-existing ``route`` envelope (#1196) is still carried, unchanged.

Communication only. It hydrates, directs, watches and executes nothing, holds no
state model of any application's objects, and reads no payload: an opaque ref
names an application transport -- never a subject, an identity or a panel. What
a message means is the applications'. How a consumer presents it -- a browser,
a log, a test -- is the consumer's.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator, Mapping, Optional

from rey_lib.errors.error_utils import AppError, ConfigError, StateError
from rey_lib.logs import AppLogger
from rey_lib.run.app_channel import IO_REQUEST_SECONDS, LaunchedApp
from rey_lib.run.async_runtime import CLOSED_ERRORS

__all__ = ["AppTransport", "AppTransports", "Listener", "TransportNotHeld"]

_logger = AppLogger(__name__)

#: The envelope an application emits to have a request routed to another application's
#: transport in the same execution context (#1196): {"route": {"destination": <transport
#: token>, "payload": {...}}, "request_id": ...}. IO_REQUEST_SECONDS, the one bound for
#: every request the IO layer carries (#1127), is rey_lib.run.app_channel's.
_ROUTE_KEY = "route"
#: How often a waiting request looks to see whether the application has ended.
_REQUEST_POLL_SECONDS = 0.2
#: The most of an ended application's last output line a failure carries.
_LAST_LINE_CHARS = 300

#: One pushed message: an application event as emitted, or ``{"stdout": text}``.
Listener = Callable[[dict[str, Any]], None]


class TransportNotHeld(AppError):
    """An opaque transport ref names no transport this execution context holds.

    Raised by :meth:`AppTransports.get`. How it is shown is the consumer's: the
    Console answers it as its own ``not_found``.
    """


class AppTransport:
    """The connection to one launched application, held by its execution context.

    Given the application AppExecute launched; :meth:`start` begins pushing
    what it emits. Subscribers are told each event the application emits and
    each chunk of its stdout, as they arrive, until it ends. It never launches,
    attaches or holds another application for the one it carries (#1189).

    A message its application emits asking to be routed to another application
    (the ``route`` envelope, #1196) is handed to ``route`` -- its AppTransports'
    routing -- instead of being pushed to subscribers. An answer to one of this
    transport's own requests goes to that request alone.
    """

    def __init__(self, app: Any,
                 route: Optional[Callable[["AppTransport", dict[str, Any]], None]] = None) -> None:
        self._listeners: list[Listener] = []
        self._guard = threading.Lock()
        self._app: Any = app
        self._pumps: list[threading.Thread] = []
        self._route = route
        # This transport's own outstanding requests, by request_id.
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}

    @property
    def run_id(self) -> Optional[int]:
        """The application's own Application Run id, once it has attached."""
        return self._app.run_id if self._app is not None else None

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Be told each pushed message; answers the call that stops it."""
        with self._guard:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._guard:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def start(self) -> None:
        """Begin pushing what the application emits."""
        for source, wrap in ((self._app.events, dict), (self._app.output, _stdout)):
            pump = threading.Thread(target=self._pump, args=(source, wrap), daemon=True)
            pump.start()
            self._pumps.append(pump)

    @property
    def exit_code(self) -> Optional[int]:
        """The application's exit code, once it has ended."""
        return self._app.exit_code if self._app is not None else None

    def ended(self) -> bool:
        """Whether the application has ended and everything it emitted has been pushed."""
        return (self._app is not None and self._app.exit_code is not None
                and not any(pump.is_alive() for pump in self._pumps))

    def connected(self, timeout: Optional[float] = None) -> bool:
        """Wait until the application has attached; False when refused or timed out."""
        return self._app is not None and self._app.connected(timeout=timeout)

    def send(self, command: Mapping[str, Any]) -> None:
        """Relay one command to the application, as given."""
        self._app.send(command)

    def request(
        self, command: Mapping[str, Any], timeout: float = IO_REQUEST_SECONDS,
    ) -> dict[str, Any]:
        """Send one command and answer the message the application pushes for it (#1127).

        Correlation and a bounded wait -- nothing else. The command's own
        ``request_id`` is kept, or one is made; the answer is the first message
        carrying that id, returned exactly as the application emitted it, an
        ``{"error": ...}`` answer included. It belongs to this request alone and is
        not pushed to subscribers (#1141 S3). What the answer means is the
        caller's: nothing here reads the payload, chooses an action or routes
        anything.

        Pending before the command is sent, so an answer however fast is never
        missed; no longer pending whatever happens.

        Raises:
            AppError: When no answer arrives within ``timeout`` seconds, or the
                application ends before answering -- naming the command asked.
        """
        request_id = str(command.get("request_id") or uuid.uuid4().hex)
        name = str(command.get("command") or "")
        answered: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._guard:
            self._pending[request_id] = answered
        try:
            self.send({**command, "request_id": request_id})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(
                        f"The application did not answer {name!r} within {timeout:g} s."
                    )
                try:
                    return answered.get(timeout=min(_REQUEST_POLL_SECONDS, remaining))
                except queue.Empty:
                    if self.ended():
                        raise AppError(
                            f"The application ended before answering {name!r}."
                        ) from None
        finally:
            with self._guard:
                self._pending.pop(request_id, None)

    def close(self) -> None:
        """Close the application through AppExecute, then release this transport's pumps.

        How the application is ended -- asked, given its grace, stopped -- is
        AppExecute's (``close`` on the launched handle); nothing here sequences it.
        """
        if self._app is None:
            return
        self._app.close()
        for pump in self._pumps:
            pump.join()

    def _pump(self, source: Callable[[], Iterator[Any]], wrap: Callable[[Any], dict]) -> None:
        """Push each item of ``source`` to every subscriber, until it ends."""
        for item in source():
            self._push(wrap(item))

    def _push(self, message: dict[str, Any]) -> None:
        """Route or answer one message, or tell every subscriber.

        In order: a ``route`` envelope goes to this transport's routing; an answer
        to one of its own pending requests to that request alone; anything else to
        every subscriber.
        """
        if self._route is not None and isinstance(message.get(_ROUTE_KEY), dict):
            self._route(self, message)
            return
        request_id = message.get("request_id")
        with self._guard:
            waiting = self._pending.get(request_id) if isinstance(request_id, str) else None
            listeners = [] if waiting is not None else list(self._listeners)
        if waiting is not None:
            waiting.put(message)
            return
        for listener in listeners:
            listener(message)


def _stdout(chunk: str) -> dict[str, Any]:
    """One chunk of the application's stdout, as a pushed message."""
    return {"stdout": chunk}


class AppTransports:
    """The transports one execution context holds, each by an opaque ref.

    The context owns the lifetime. A transport lives until it is closed, its
    application ends, or the context holding it ends -- then every one it holds
    is closed (``close_all``, #1189). A consumer's stream ending releases only
    that stream.

    It also routes one application's request to another application it holds
    (#1196): a request one transport's application emits is sent, under the
    asker's own request_id, to the transport its ``destination`` token names, and
    the answer is sent back to the asker. Routing reads only the envelope, the
    request_id and the ref; what the request and its answer mean is the
    applications'.
    """

    def __init__(self) -> None:
        self._held: dict[str, AppTransport] = {}
        self._guard = threading.Lock()

    def open(self, app: Any) -> tuple[str, AppTransport]:
        """Hold a new transport for the launched ``app``, not yet started; answer its ref."""
        ref = uuid.uuid4().hex
        transport = AppTransport(app, route=self._route)
        with self._guard:
            self._held[ref] = transport
        return ref, transport

    def get(self, ref: str) -> AppTransport:
        """The transport held as ``ref``.

        Raises:
            TransportNotHeld: If this context holds no such transport.
        """
        with self._guard:
            transport = self._held.get(str(ref or ""))
        if transport is None:
            raise TransportNotHeld("No application transport is held under that ref.")
        return transport

    def launch(
        self, runtime: Any, application: str, operation: str, inputs: Mapping[str, Any],
        listener: Listener,
    ) -> tuple[str, AppTransport, Callable[[], None]]:
        """Launch one application operation and hold its transport, started (#1138).

        AppExecute launches the operation from its semantic inputs
        (``LaunchedApp.launch_operation``), this context holds the transport,
        ``listener`` is subscribed before anything is pushed, and the pumps start.

        Returns:
            The transport's ref, the transport, and the call that unsubscribes
            ``listener``.

        Raises:
            AppError: When the application cannot be launched.
        """
        launched = LaunchedApp.launch_operation(runtime, application, operation, dict(inputs))
        ref, transport = self.open(launched)
        release = transport.subscribe(listener)
        transport.start()
        return ref, transport, release

    def establish(
        self, runtime: Any, application: str, operation: str, inputs: Mapping[str, Any],
        *, timeout: float = IO_REQUEST_SECONDS,
    ) -> dict[str, Any]:
        """Launch one application operation and await its first projection (#1138).

        A hosted application sends its projection first (recipe
        app.serve_a_live_object_to_a_panel step 4). Both go back unchanged -- the
        ref a consumer attaches to, and the projection. Nothing here reads it.

        Returns:
            ``{"token": <transport ref>, "projection": <the first projection>}``,
            retaining its opaque ``object`` reference when the application names one.

        Raises:
            AppError: When the application cannot be launched, ends before sending a
                projection, or sends none within ``timeout``; it is closed then.
        """
        pushed: queue.Queue[dict[str, Any]] = queue.Queue()
        ref, transport, release = self.launch(
            runtime, application, operation, inputs, pushed.put)
        # The last line the application wrote, kept only to say why it ended (#1171).
        last_line = ""
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(f"The application sent no projection within {timeout:g} s.")
                try:
                    message = pushed.get(timeout=min(_REQUEST_POLL_SECONDS, remaining))
                except queue.Empty:
                    if transport.ended():
                        raise AppError(
                            "The application ended before sending its projection "
                            f"(exit code {transport.exit_code})"
                            + (f": {last_line}" if last_line else ".")
                        ) from None
                    continue
                if "projection" in message:
                    return {"token": ref, "projection": message["projection"],
                            **({"object": message["object"]} if "object" in message else {})}
                last_line = _last_line(message.get("stdout"), last_line)
        except AppError:
            self.close(ref)
            raise
        finally:
            release()

    def close(self, ref: str) -> None:
        """Close and drop the transport held as ``ref``."""
        with self._guard:
            transport = self._held.pop(str(ref or ""), None)
        if transport is not None:
            transport.close()

    def close_all(self) -> None:
        """Close and drop every transport this context holds: the context has ended.

        Dropped first, all at once under the guard, so from the moment the
        context ends nothing can reattach to a transport being closed, and a
        stream's own cleanup closing one by its ref finds it already gone.

        Then closed together. The transports are independent applications, so
        each closes on its own worker and keeps its own close -- asked, given its
        grace, stopped -- and the context's end takes as long as the slowest of
        them rather than the sum. Every one is attempted; a failure is raised
        once all have returned.
        """
        with self._guard:
            held = list(self._held.values())
            self._held.clear()
        if not held:
            return
        with ThreadPoolExecutor(max_workers=len(held),
                                thread_name_prefix="app-transport-close") as closing:
            closed = [closing.submit(transport.close) for transport in held]
        for one in closed:
            one.result()

    def _route(self, source: AppTransport, message: dict[str, Any]) -> None:
        """Route one application's request on its own worker, so the asker's pump never waits."""
        threading.Thread(target=self._forward, args=(source, message),
                         name="app-transport-route", daemon=True).start()

    def _forward(self, source: AppTransport, message: dict[str, Any]) -> None:
        """Send a request to the transport it names and its answer back to the asker (#1196).

        The asker's request_id is kept end to end. A destination this context does
        not hold, a payload that is not a command, a target that does not answer
        within IO_REQUEST_SECONDS or ends first -- each is answered to the asker as
        ``{"error": ..., "request_id": ...}``. An asker that has ended is not
        answered: its request ended with it, and nothing is kept.
        """
        request_id = message.get("request_id")
        if not request_id:
            # Nothing could carry an answer back; the request is malformed at its source.
            _logger.warning("A routed request arrived without a request_id and was not routed.")
            return
        route = message.get(_ROUTE_KEY) or {}
        payload = route.get("payload")
        try:
            if not isinstance(payload, dict):
                raise ConfigError("A routed request carries one command as its payload.")
            destination = self.get(str(route.get("destination") or ""))
            answer = {"answer": destination.request({**payload, "request_id": request_id}),
                      "request_id": request_id}
        except TransportNotHeld:
            answer = {"error": "No application with that transport is held here.",
                      "request_id": request_id}
        except AppError as refused:
            answer = {"error": str(refused), "request_id": request_id}
        _answer(source, answer)


def _answer(source: AppTransport, answer: dict[str, Any]) -> None:
    """Send ``answer`` to the asking transport, unless its application has ended."""
    if source.ended():
        return
    try:
        source.send(answer)
    except (StateError, *CLOSED_ERRORS):
        # The asker ended between the check and the send; nothing waits for it.
        return


def _last_line(chunk: Any, previous: str) -> str:
    """The last non-empty line of an output chunk, bounded; ``previous`` when it has none."""
    lines = [line.strip() for line in str(chunk or "").splitlines() if line.strip()]
    return lines[-1][:_LAST_LINE_CHARS] if lines else previous
