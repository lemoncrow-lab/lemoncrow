"""A loopback that behaves like a network, for measurements that mean something.

A number measured against an in-process server on loopback answers "how fast is
the code", which is a useful question but not the one a developer is asking. The
one they are asking is "how long does my session take against the server IT
runs", and between here and there sit a round trip and a TLS handshake -- paid
once per *connection*, and this client opens a connection per request.

Two pieces, both standard library:

:class:`LatencyProxy`
    A TCP relay that holds every byte for half the configured round trip in each
    direction. It is a *pipelining* delay, not a per-chunk stall: a reader thread
    stamps each chunk with the instant it may be released and a writer thread
    releases it then, so a large body still costs one half-trip and not one per
    chunk. Delaying each chunk in line would make every measurement taken through
    it an overstatement, which is the opposite of what it is for.

:func:`self_signed`
    A certificate for ``127.0.0.1`` from the ``openssl`` binary, and the client
    ``SSLContext`` that trusts exactly it. Nothing here weakens verification --
    the point is to measure a real handshake, and a client that skipped
    verification would not be doing the work being measured.

Nothing in this module is imported by ``lemoncrow_client``; it is test
scaffolding and lives with the tests.
"""

from __future__ import annotations

import queue
import shutil
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["LatencyProxy", "TlsMaterial", "openssl_available", "self_signed"]

_CHUNK: Final[int] = 64 * 1024
_SHUTDOWN = object()


class LatencyProxy:
    """A TCP relay that adds a fixed round-trip time in front of a server."""

    def __init__(self, upstream_host: str, upstream_port: int, *, rtt_s: float) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._half = rtt_s / 2.0
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(128)
        self._port = int(self._listener.getsockname()[1])
        self._running = True
        self._threads: list[threading.Thread] = []
        self._accepting = threading.Thread(target=self._accept_forever, daemon=True, name="rtt-accept")
        self._accepting.start()

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self._port}"

    def __enter__(self) -> LatencyProxy:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def close(self) -> None:
        self._running = False
        try:
            self._listener.close()
        except OSError:
            pass
        self._accepting.join(timeout=5.0)
        for thread in self._threads:
            thread.join(timeout=5.0)

    def _accept_forever(self) -> None:
        while self._running:
            try:
                downstream, _address = self._listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._serve, args=(downstream,), daemon=True, name="rtt-conn")
            worker.start()
            self._threads.append(worker)
            self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _serve(self, downstream: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self._upstream, timeout=30.0)
        except OSError:
            downstream.close()
            return
        downstream.settimeout(60.0)
        upstream.settimeout(60.0)
        pumps = [
            _Pump(downstream, upstream, self._half),
            _Pump(upstream, downstream, self._half),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()
        for handle in (downstream, upstream):
            try:
                handle.close()
            except OSError:
                pass


class _Pump:
    """One direction of one connection: read, stamp, release at the deadline."""

    def __init__(self, source: socket.socket, sink: socket.socket, delay_s: float) -> None:
        self._source = source
        self._sink = sink
        self._delay = delay_s
        self._queue: queue.Queue[object] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True, name="rtt-read")
        self._writer = threading.Thread(target=self._write, daemon=True, name="rtt-write")

    def start(self) -> None:
        self._reader.start()
        self._writer.start()

    def join(self) -> None:
        self._reader.join(timeout=120.0)
        self._writer.join(timeout=120.0)

    def _read(self) -> None:
        try:
            while True:
                block = self._source.recv(_CHUNK)
                if not block:
                    break
                self._queue.put((time.monotonic() + self._delay, block))
        except OSError:
            pass
        finally:
            self._queue.put(_SHUTDOWN)

    def _write(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    break
                assert isinstance(item, tuple)
                release, block = item
                remaining = release - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                self._sink.sendall(block)
        except OSError:
            pass
        finally:
            try:
                self._sink.shutdown(socket.SHUT_WR)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """A server certificate and key, and the context a client verifies it with."""

    certfile: Path
    keyfile: Path

    def client_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.certfile))
        context.check_hostname = False  # the certificate names an IP, not a name
        context.verify_mode = ssl.CERT_REQUIRED
        return context


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def self_signed(directory: Path) -> TlsMaterial:
    """A one-day self-signed certificate for ``127.0.0.1``."""
    binary = shutil.which("openssl")
    if binary is None:  # pragma: no cover - guarded by openssl_available
        raise RuntimeError("openssl is not on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    completed = subprocess.run(
        [
            binary,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(keyfile),
            "-out",
            str(certfile),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - environment failure
        raise RuntimeError(f"openssl failed: {completed.stderr}")
    keyfile.chmod(0o600)
    return TlsMaterial(certfile=certfile, keyfile=keyfile)
