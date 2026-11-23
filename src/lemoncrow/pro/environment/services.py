"""Declared services kept up for the length of one run (``environment.toml`` ``[services]``).

A check that ``needs`` services runs only while each is up: :class:`ServicesRunner`
starts them in declaration order, waits until each one's ``ready`` probe passes (one
deadline per service), keeps them up while the caller's block runs and always stops
them in reverse order, also when a start, a probe or the block fails. How a service
runs is a :class:`ServiceBackend`; a run without one is refused
(:func:`service_refusal`), never run against whatever happens to listen.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Protocol

from lemoncrow_client.environment import Service

#: How long one service may take to pass its ``ready`` probe.
READY_TIMEOUT_SECONDS: Final[float] = 120.0
READY_INTERVAL_SECONDS: Final[float] = 0.5


class ServiceUnavailable(RuntimeError):
    """A declared service did not start or did not become ready; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class StartedService:
    """One running service: the backend's handle and the environment a check gets for it."""

    name: str
    handle: object = None
    env: tuple[tuple[str, str], ...] = ()


class ServiceBackend(Protocol):
    """Starts, probes and stops one service image where the run can reach it."""

    def start(self, service: Service) -> StartedService: ...

    def probe(self, started: StartedService, service: Service) -> bool:
        """Run the service's ``ready`` probe once: ``True`` once it exits 0."""
        ...

    def stop(self, started: StartedService) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceSet:
    """The services one trusted recipe needs; only a trusted dispatcher builds one (never YAML)."""

    services: tuple[Service, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(service.name for service in self.services)


def service_refusal(services: Sequence[Service], backend: ServiceBackend | None) -> str:
    """Why a run that needs *services* cannot start, or ``""``.

    Review starts a service only from an image pinned by digest, and only with a backend.
    """

    if not services:
        return ""
    unpinned = [service.name for service in services if not service.digest]
    if unpinned:
        return (
            f"services {', '.join(unpinned)} are not pinned by digest; "
            "a service starts only from an image@sha256 reference"
        )
    if backend is None:
        # lc-debt: the bubblewrap/seatbelt run stage has no service backend (it runs no images), so there a check
        # that needs services is NOT_RUN; with a pinned [toolchain] and a rootless OCI engine Review runs them on the
        # run's internal network. Upgrade: unpack the pinned service images inside the bubblewrap run netns.
        names = ", ".join(service.name for service in services)
        return (
            f"needs services ({names}); this executor has no service backend, so the check is not run "
            "(services run with a pinned [toolchain] image under a rootless container engine)"
        )
    return ""


class ServicesRunner:
    """Keeps declared services up through one block; see the module docstring."""

    def __init__(
        self,
        backend: ServiceBackend,
        *,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        interval: float = READY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backend = backend
        self._ready_timeout = ready_timeout
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        #: ``name: error`` for each service whose stop failed in the last :meth:`up`.
        self.stop_errors: tuple[str, ...] = ()

    def _wait(self, started: StartedService, service: Service) -> None:
        deadline = self._clock() + self._ready_timeout
        while True:
            try:
                if self._backend.probe(started, service):
                    return
            except Exception as exc:
                raise ServiceUnavailable(f"service {service.name}: its ready probe could not run: {exc}") from exc
            if self._clock() >= deadline:
                raise ServiceUnavailable(f"service {service.name} was not ready within {self._ready_timeout:g} s")
            self._sleep(self._interval)

    def start_all(self, services: Sequence[Service]) -> tuple[StartedService, ...]:
        """Start and await every service in order; on any failure stop the started ones and raise.

        For a lifetime longer than one block (a Workspaces task keeps its services until it finishes); the
        caller closes them with :meth:`stop_all`.
        """

        started: list[StartedService] = []
        try:
            for service in services:
                try:
                    current = self._backend.start(service)
                except ServiceUnavailable:
                    raise
                except Exception as exc:
                    raise ServiceUnavailable(f"service {service.name} could not start: {exc}") from exc
                started.append(current)
                self._wait(current, service)
        except BaseException:
            self.stop_all(started)
            raise
        return tuple(started)

    def stop_all(self, started: Sequence[StartedService]) -> tuple[str, ...]:
        """Stop *started* in reverse order, every one even when another fails; ``name: error`` per failure."""

        errors: list[str] = []
        for current in reversed(started):
            try:
                self._backend.stop(current)
            except Exception as exc:
                errors.append(f"{current.name}: {exc}"[:300])
        self.stop_errors = tuple(errors)
        return self.stop_errors

    @contextmanager
    def up(self, services: Sequence[Service]) -> Iterator[tuple[StartedService, ...]]:
        """Start and await every service, yield them, then stop them all in reverse order."""

        self.stop_errors = ()
        started = self.start_all(services)
        try:
            yield started
        finally:
            self.stop_all(started)


def services_env(started: Sequence[StartedService]) -> tuple[tuple[str, str], ...]:
    """The environment a check gets from its running services, in start order."""

    return tuple(pair for service in started for pair in service.env)


__all__ = [
    "READY_INTERVAL_SECONDS",
    "READY_TIMEOUT_SECONDS",
    "ServiceBackend",
    "ServiceSet",
    "ServiceUnavailable",
    "ServicesRunner",
    "StartedService",
    "service_refusal",
    "services_env",
]
