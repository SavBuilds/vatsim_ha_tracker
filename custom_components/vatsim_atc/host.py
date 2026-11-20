"""Runs the engine against an asyncio event loop. No Home Assistant imports, so it is testable
on a plain PC; __init__.py supplies the Home Assistant pieces (HTTP client, dispatcher, services).

Threading model. The engine was written for a host that runs every callback on one thread and
lets the engine block there (the feed download happens inside the poll). So:
  - the event loop only keeps timers and hands work over; engine code never runs on it;
  - one worker thread runs every engine callback, in order, which preserves the engine's
    single-thread assumption;
  - the engine's own loader threads (boundaries, navdata, NAT tracks, METAR) hand their results
    back through run_in(..., 0), which lands on that same worker.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .core.engine import FetchError, Host, VatsimAtcEngine

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.DEBUG, "WARNING": logging.WARNING,
          "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
# How long past the request's own timeout to wait for the loop to answer.
FETCH_GRACE_SECONDS = 5.0

AsyncFetch = Callable[[str, float, Optional[dict]], Awaitable[str]]


class LoopHost(Host):
    def __init__(
        self, args: dict, loop: asyncio.AbstractEventLoop, async_fetch: AsyncFetch,
        on_publish: Callable[[str], None],
        async_call_service: Optional[Callable[[str, dict], Awaitable[Any]]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.args = dict(args)
        self._loop = loop
        self._async_fetch = async_fetch
        self._on_publish = on_publish            # called on the loop with the engine's entity id
        self._async_call_service = async_call_service
        self._logger = logger or logging.getLogger(__name__)
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="vatsim_atc")
        self._timers: set = set()                # touched only on the loop
        self._stopped = threading.Event()
        self._initialized = False                # the engine's initialize() has completed
        self._listeners: dict[str, list] = {}
        self._local_states: dict[str, Any] = {}
        # What entities read: engine entity id -> (state, attributes). Replaced whole on each
        # publish, never mutated, so a reader on the loop always sees a consistent pair.
        self.published: dict[str, tuple[Any, dict]] = {}

    # ---- Host contract -----------------------------------------------------------------

    def log(self, message: str, level: str = "INFO", **_: Any) -> None:
        # The engine logs every poll at INFO. That is debug detail in a Home Assistant log.
        self._logger.log(LEVELS.get(str(level).upper(), logging.DEBUG), "%s", message)

    def set_state(self, entity_id: str, **kwargs: Any) -> Any:
        # The engine keeps the merged attributes (a publish adds to what was there); show those.
        merged = getattr(self, "_published_states", {}).get(entity_id, (None, kwargs.get("attributes") or {}))[1]
        attributes = {k: v for k, v in merged.items() if v is not None}
        self.published = {**self.published, entity_id: (kwargs.get("state"), attributes)}
        self._to_loop(self._on_publish, entity_id)
        return kwargs

    def get_state(self, entity_id: Optional[str] = None, **_: Any) -> Any:
        return self._local_states.get(entity_id)

    def render_template(self, template: str, **_: Any) -> Any:
        return None

    def call_service(self, service: str, **kwargs: Any) -> Any:
        if self._async_call_service is not None and not self._stopped.is_set():
            asyncio.run_coroutine_threadsafe(self._async_call_service(service, kwargs), self._loop)

    def run_in(self, callback: Callable, delay: float, **kwargs: Any) -> Any:
        self._to_loop(self._arm, lambda: callback(kwargs), float(delay), None)

    def run_every(self, callback: Callable, start: Any, interval: float, **kwargs: Any) -> Any:
        first = max(0.0, (start - datetime.now()).total_seconds()) if isinstance(start, datetime) else 0.0
        self._to_loop(self._arm, lambda: callback(kwargs), first, float(interval))

    def listen_state(self, callback: Callable, entity_id: str, **_: Any) -> Any:
        self._listeners.setdefault(entity_id, []).append(callback)

    def datetime(self) -> datetime:
        return datetime.now()

    def http_get(self, url: str, timeout: float, headers: Optional[dict] = None) -> str:
        if self._stopped.is_set():
            raise FetchError("the integration is stopping", kind="Stopped")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise RuntimeError("http_get called on the event loop; it would wait for itself")
        future = asyncio.run_coroutine_threadsafe(self._async_fetch(url, timeout, headers), self._loop)
        try:
            return future.result(timeout + FETCH_GRACE_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise FetchError(f"no answer within {timeout:g} s", kind="ReadTimeout", is_timeout=True) from exc
        except FetchError:
            raise
        except Exception as exc:  # whatever the HTTP client raised, reported the engine's way
            raise FetchError(str(exc) or type(exc).__name__, kind=type(exc).__name__,
                             is_timeout=isinstance(exc, (asyncio.TimeoutError, TimeoutError))) from exc

    # ---- Used by the integration ---------------------------------------------------------

    def set_local_state(self, entity_id: str, value: Any) -> None:
        """Record a value the engine reads with get_state, and tell its listeners. Any thread."""
        old = self._local_states.get(entity_id)
        self._local_states[entity_id] = value
        for callback in list(self._listeners.get(entity_id, ())):
            self._submit(lambda cb=callback: cb(entity_id, "state", old, value, {}))

    def start(self) -> None:
        """Run the engine's initialize() on the worker. Returns at once."""
        def boot() -> None:
            self.initialize()
            self._initialized = True
        self._submit(boot)

    def shutdown(self) -> None:
        """Stop timers and the worker, then let the engine save its state. Blocking; call it
        from an executor, not from the loop. Safe to call twice."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._loop.call_soon_threadsafe(self._cancel_timers)
        except RuntimeError:
            pass  # the loop is already closed; its timers went with it
        self._worker.shutdown(wait=True, cancel_futures=True)
        if not self._initialized:
            return  # stopped before the engine set itself up: nothing to save
        try:
            self.terminate()
        except Exception:
            self._logger.exception("Saving state at shutdown failed")

    # ---- Internals -------------------------------------------------------------------------

    def _to_loop(self, function: Callable, *args: Any) -> None:
        """Run function(*args) on the loop, from any thread. Dropped once stopped or the loop is closed."""
        if self._stopped.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(function, *args)
        except RuntimeError:
            pass

    def _arm(self, call: Callable[[], Any], delay: float, repeat: Optional[float]) -> None:
        """On the loop: after delay seconds, hand call to the worker; repeat if asked."""
        if self._stopped.is_set():
            return
        handle = None

        def fire() -> None:
            self._timers.discard(handle)
            self._submit(call)
            if repeat is not None:
                self._arm(call, max(repeat, 0.01), repeat)

        handle = self._loop.call_later(max(delay, 0.0), fire)
        self._timers.add(handle)

    def _cancel_timers(self) -> None:
        for handle in list(self._timers):
            handle.cancel()
        self._timers.clear()

    def _submit(self, call: Callable[[], Any]) -> None:
        if self._stopped.is_set():
            return
        try:
            self._worker.submit(self._guarded, call)
        except RuntimeError:
            pass  # the worker shut down between the check and the submit

    def _guarded(self, call: Callable[[], Any]) -> None:
        if self._stopped.is_set():
            return
        try:
            call()
        except Exception:
            self._logger.exception("Unhandled error in the tracker engine")


class Tracker(VatsimAtcEngine, LoopHost):
    """The engine on a LoopHost. Engine first, so its set_state wraps the host's."""

    def __init__(self, *args: Any, last_aircraft_file: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_aircraft_file = last_aircraft_file

    def check_config(self) -> None:
        """Raise ValueError now if the engine would refuse the configuration."""
        self._load_config()

    def _last_aircraft_path(self) -> str:
        # The engine's default is next to its own file, which an update would wipe.
        return self._last_aircraft_file

    def _reconcile_published_states(self, now: float) -> None:
        # A host that writes states from outside Home Assistant has to check they were held
        # and write them again after a restart. Real entities hold their own state.
        return None
