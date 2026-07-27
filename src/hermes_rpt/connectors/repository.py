"""Repositories for the connector bounded context (metadata only — see Phase 4 for actual
pooled connections)."""

from __future__ import annotations

from hermes_rpt.common.repository import TenantScopedRepository
from hermes_rpt.connectors.models import CustomerDatabaseConnection, DatabaseCredentialReference


class DatabaseCredentialReferenceRepository(TenantScopedRepository[DatabaseCredentialReference]):
    model = DatabaseCredentialReference


class CustomerDatabaseConnectionRepository(TenantScopedRepository[CustomerDatabaseConnection]):
    model = CustomerDatabaseConnection
