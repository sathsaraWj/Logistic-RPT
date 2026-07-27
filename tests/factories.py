"""Lightweight test factories (Phase 2 requirement 11).

Deliberately not using a framework like factory_boy — the platform is young enough that plain
functions returning unpersisted ORM instances with sensible random defaults are clearer than
adding a new dependency. Callers persist the returned instance via a repository.
"""

from __future__ import annotations

import uuid

from hermes_rpt.tenants.models import Tenant, User


def make_tenant(*, name: str | None = None, slug: str | None = None) -> Tenant:
    unique = uuid.uuid4().hex[:8]
    return Tenant(
        name=name or f"Test Tenant {unique}",
        slug=slug or f"test-tenant-{unique}",
    )


def make_user(*, email: str | None = None, display_name: str = "Test User") -> User:
    unique = uuid.uuid4().hex[:8]
    return User(
        email=email or f"user-{unique}@example.com",
        display_name=display_name,
    )
