"""Plain terminal progress reporting for long-running CLI commands."""

from __future__ import annotations

import sys
import time
from threading import Event, Lock, Thread
from typing import TextIO


class ProgressReporter:
    """Print progress with elapsed time and a simple ETA."""

    def __init__(
        self,
        suite: str,
        total: int | None = None,
        heartbeat_seconds: int = 30,
        *,
        in_place: bool = True,
        stream: TextIO | None = None,
    ) -> None:
        self.suite = suite
        self.total = total if total and total > 0 else None
        self.done = 0
        self.started_at = time.monotonic()
        self.current = ""
        self._last_title = "running"
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = Event()
        self._lock = Lock()
        self._heartbeat: Thread | None = None
        self._in_place = in_place
        self._stream = stream or sys.stderr
        self._line_active = False

    def start(self, title: str, *, current: str = "") -> None:
        self.current = current
        self._emit(title)
        self._start_heartbeat()

    def phase(self, title: str, *, current: str = "") -> None:
        self.current = current
        self._emit(title)

    def step(self, title: str, *, current: str = "", advance: int = 1) -> None:
        self.done += max(advance, 0)
        self.current = current
        self._emit(title)

    def finish(self, title: str = "complete") -> None:
        self._stop_heartbeat()
        if self.total is not None:
            self.done = max(self.done, self.total)
        self._emit(title)
        self._finish_line()

    def _emit(self, title: str) -> None:
        with self._lock:
            self._last_title = title
            elapsed = max(time.monotonic() - self.started_at, 0.0)
            parts = [f"[{self.suite}] {title}"]
            if self.total is not None:
                pct = min(100.0, self.done / self.total * 100)
                parts.append(f"{self.done}/{self.total} ({pct:.0f}%)")
                parts.append(_bar(self.done, self.total))
            elif self.done:
                parts.append(f"{self.done} done")
            parts.append(f"elapsed {_fmt_duration(elapsed)}")
            if self.total is not None and self.done > 0 and self.done < self.total:
                avg = elapsed / self.done
                eta = avg * (self.total - self.done)
                parts.append(f"eta {_fmt_duration(eta)}")
            if self.current:
                parts.append(f"current {self.current}")
            line = " | ".join(parts)
            if self._in_place:
                self._stream.write("\r\x1b[2K" + line)
                self._stream.flush()
                self._line_active = True
            else:
                print(line, file=self._stream, flush=True)

    def _finish_line(self) -> None:
        if not self._in_place:
            return
        with self._lock:
            if self._line_active:
                self._stream.write("\n")
                self._stream.flush()
                self._line_active = False

    def _start_heartbeat(self) -> None:
        if self._heartbeat_seconds <= 0 or self._heartbeat is not None:
            return
        self._heartbeat = Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            self._emit(self._last_title)


def _bar(done: int, total: int, width: int = 20) -> str:
    filled = min(width, int(width * done / max(total, 1)))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _fmt_duration(seconds: float) -> str:
    seconds = round(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minute:02d}:{sec:02d}"
    return f"{minute:02d}:{sec:02d}"
