"""A dual-write `SecretProvider` for the Phase 17 demo CLI only.

`hermes_rpt.secrets.provider.LocalDevSecretProvider` is an in-memory, `@lru_cache`d
process-wide singleton — exactly right for the app's own request lifecycle, with one wrinkle
this demo runs into twice over:

1. Each `make demo-*` step is a *separate* process invocation. A connection's secret
   registered by `demo-discover` would be unresolvable in `demo-train`'s own, brand-new
   process — `LocalDevSecretProvider`'s cache never survives that.
2. *Within* `demo-discover`'s own single process, schema discovery's background job
   (`hermes_rpt.schemas.jobs.run_discovery_job`, scheduled via `asyncio.create_task` from
   `apps/api/routers/schema_discovery.py`) calls `hermes_rpt.secrets.provider.
   get_secret_provider()` directly — a plain function call, not a FastAPI `Depends(...)`, so
   `app.dependency_overrides` can never reach it. Overriding the connection-lifecycle-manager
   dependency alone (to redirect *registration* through a cross-process-persistent store)
   would leave that same-process background job resolving against the real cache, which
   registration never touched.

`DualWriteSecretProvider` solves both at once: every `store()` writes to a local, gitignored
JSON file under `data/` (for #1) *and* directly into the real, cached `LocalDevSecretProvider`
instance's own store (for #2) under the same reference key; `resolve()` checks the real
in-memory store first (fast path, covers the same-process background job) and falls back to
the file (covers a later process). Still entirely local-dev-only — never touches a real secret
store, just makes `LocalDevSecretProvider`'s reach span what this demo's multi-process,
multi-pathway shape actually needs.

That still leaves one gap `store()` alone can't close: a background job triggered against a
connection that was *registered in an earlier process* (e.g. `demo-security-test` re-running
discovery on a connection `demo-discover` created) never calls `store()` in *this* process at
all, so the real provider's cache stays empty for it — `seed_real_provider_from_file()` closes
that by pre-loading every key the file already knows about into the real cache before any
request is made, so a same-process background job can resolve a secret regardless of which
earlier process actually stored it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from hermes_rpt.secrets.provider import SecretNotFoundError, get_secret_provider

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SECRETS_FILE = _REPO_ROOT / "data" / "demo_runs" / ".secrets.json"


class DualWriteSecretProvider:
    def __init__(self, path: Path = _SECRETS_FILE) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")

    async def store(self, *, secret_value: str) -> str:
        reference_key = f"demo-file:{uuid.uuid4()}"
        async with self._lock:
            data = self._load()
            data[reference_key] = secret_value
            self._save(data)
        # Also seed the real, process-cached provider under the same key, so a same-process
        # caller that goes around FastAPI's dependency injection entirely (the schema-discovery
        # background job) can still resolve it — see this module's docstring, point 2. Reaches
        # into its private `_secrets` dict directly (no public "seed under a given key" method
        # exists, nor should one — this is a demo-only workaround, not a real usage pattern).
        get_secret_provider()._secrets[reference_key] = secret_value  # type: ignore[attr-defined] # noqa: SLF001
        return reference_key

    async def resolve(self, reference_key: str) -> str:
        real_provider = get_secret_provider()
        try:
            return await real_provider.resolve(reference_key)
        except SecretNotFoundError:
            pass
        async with self._lock:
            data = self._load()
        try:
            return data[reference_key]
        except KeyError as exc:
            raise SecretNotFoundError(reference_key) from exc

    async def delete(self, reference_key: str) -> None:
        async with self._lock:
            data = self._load()
            data.pop(reference_key, None)
            self._save(data)
        await get_secret_provider().delete(reference_key)


def seed_real_provider_from_file(path: Path = _SECRETS_FILE) -> None:
    """Pre-loads every secret the file already knows about into the real, process-cached
    `LocalDevSecretProvider` — call once per process, before any request that might schedule a
    background job (schema discovery), so a connection registered in an *earlier* demo process
    is still resolvable by this process's own background job. See this module's docstring."""

    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    real_provider = get_secret_provider()
    for reference_key, secret_value in data.items():
        real_provider._secrets[reference_key] = secret_value  # type: ignore[attr-defined] # noqa: SLF001
