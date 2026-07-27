"""Timeout, retry, and circuit-breaker interfaces (Phase 13) — generic, reusable resilience
primitives for calls to something that can be slow or transiently fail. Used by
`hermes_rpt.inference.model_loading` around the one genuinely "external dependency" the
inference flow has (loading a model artifact from the MLflow tracking store), but nothing here
is inference-specific.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class TimeoutExceededError(Exception):
    pass


async def with_timeout[T](coro: Awaitable[T], *, seconds: float) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except TimeoutError as exc:
        raise TimeoutExceededError(f"Operation exceeded {seconds}s timeout") from exc


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.1
    backoff_multiplier: float = 2.0


async def retry_with_backoff[T](
    operation: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig,
    retryable: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retries `operation` up to `config.max_attempts` times with exponential backoff. Only
    exceptions matching `retryable` are retried — everything else propagates immediately, since
    retrying a programming error or an authorization failure is never correct."""

    delay = config.initial_backoff_seconds
    last_error: Exception | None = None
    for attempt in range(1, config.max_attempts + 1):
        try:
            return await operation()
        except retryable as exc:
            last_error = exc
            if attempt == config.max_attempts:
                break
            await asyncio.sleep(delay)
            delay *= config.backoff_multiplier
    assert last_error is not None  # nosec B101 - loop always sets it before falling through
    raise last_error


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast, not calling the dependency
    HALF_OPEN = "half_open"  # one trial call allowed, to test recovery


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    """A minimal closed/open/half-open circuit breaker (not thread-safe across processes — an
    in-process guard against hammering a struggling dependency, same scope as
    `hermes_rpt.connectors.pool_registry`'s per-process pooling)."""

    def __init__(self, *, failure_threshold: int = 5, reset_timeout_seconds: float = 30.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_seconds = reset_timeout_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        elapsed_since_open = self._opened_at is not None and (
            time.monotonic() - self._opened_at >= self._reset_timeout_seconds
        )
        if self._state == CircuitState.OPEN and elapsed_since_open:
            self._state = CircuitState.HALF_OPEN
        return self._state

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        current_state = self.state
        if current_state == CircuitState.OPEN:
            raise CircuitOpenError("Circuit is open — failing fast without calling the dependency")

        try:
            result = await operation()
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def _on_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
