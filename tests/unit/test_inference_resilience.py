"""Unit tests for the generic timeout/retry/circuit-breaker primitives (Phase 13) in isolation,
independent of anything inference- or model-loading-specific.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_rpt.inference.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryConfig,
    TimeoutExceededError,
    retry_with_backoff,
    with_timeout,
)

# --- with_timeout --------------------------------------------------------------------------------


async def test_with_timeout_returns_the_result_when_fast_enough() -> None:
    async def _fast() -> int:
        return 42

    assert await with_timeout(_fast(), seconds=1.0) == 42


async def test_with_timeout_raises_when_the_operation_is_too_slow() -> None:
    async def _slow() -> int:
        await asyncio.sleep(1.0)
        return 42

    with pytest.raises(TimeoutExceededError):
        await with_timeout(_slow(), seconds=0.01)


# --- retry_with_backoff --------------------------------------------------------------------------


async def test_retry_with_backoff_returns_on_first_success_without_retrying() -> None:
    calls = 0

    async def _operation() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_with_backoff(_operation, config=RetryConfig(max_attempts=3))
    assert result == "ok"
    assert calls == 1


async def test_retry_with_backoff_retries_up_to_max_attempts_then_raises() -> None:
    calls = 0

    async def _always_fails() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await retry_with_backoff(
            _always_fails,
            config=RetryConfig(max_attempts=3, initial_backoff_seconds=0.0),
        )
    assert calls == 3


async def test_retry_with_backoff_succeeds_after_transient_failures() -> None:
    calls = 0

    async def _fails_twice_then_succeeds() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")
        return "ok"

    result = await retry_with_backoff(
        _fails_twice_then_succeeds,
        config=RetryConfig(max_attempts=5, initial_backoff_seconds=0.0),
    )
    assert result == "ok"
    assert calls == 3


async def test_retry_with_backoff_does_not_retry_non_retryable_exceptions() -> None:
    calls = 0

    async def _operation() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        await retry_with_backoff(
            _operation,
            config=RetryConfig(max_attempts=3, initial_backoff_seconds=0.0),
            retryable=(RuntimeError,),
        )
    assert calls == 1


# --- CircuitBreaker --------------------------------------------------------------------------


async def test_circuit_breaker_starts_closed() -> None:
    breaker = CircuitBreaker()
    assert breaker.state == CircuitState.CLOSED


async def test_circuit_breaker_opens_after_reaching_the_failure_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=60.0)

    async def _fails() -> None:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fails)

    assert breaker.state == CircuitState.OPEN


async def test_open_circuit_fails_fast_without_calling_the_operation() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=60.0)
    calls = 0

    async def _fails() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await breaker.call(_fails)
    assert breaker.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await breaker.call(_fails)
    assert calls == 1  # the second call never reached the operation


async def test_circuit_breaker_transitions_to_half_open_after_reset_timeout() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.01)

    async def _fails() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await breaker.call(_fails)
    assert breaker.state == CircuitState.OPEN

    await asyncio.sleep(0.02)
    assert breaker.state == CircuitState.HALF_OPEN


async def test_a_success_after_half_open_closes_the_circuit_and_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.01)

    async def _fails() -> None:
        raise RuntimeError("boom")

    async def _succeeds() -> str:
        return "ok"

    with pytest.raises(RuntimeError):
        await breaker.call(_fails)
    await asyncio.sleep(0.02)
    assert breaker.state == CircuitState.HALF_OPEN

    result = await breaker.call(_succeeds)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED

    # Confirms the failure count was actually reset, not just the state flag: it now takes a
    # fresh `failure_threshold` failures to reopen, not just one more.
    with pytest.raises(RuntimeError):
        await breaker.call(_fails)
    assert breaker.state == CircuitState.OPEN
