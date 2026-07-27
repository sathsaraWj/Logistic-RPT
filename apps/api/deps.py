"""Shared, non-auth FastAPI dependencies for apps/api routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.common.db import get_session
from hermes_rpt.connectors.pool_registry import get_pool_registry
from hermes_rpt.connectors.postgres import PostgresConnector
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.schemas.service import SchemaDiscoveryService
from hermes_rpt.secrets.provider import get_secret_provider

DbSessionDep = Annotated[AsyncSession, Depends(get_session)]

_connector = PostgresConnector()


def get_connection_lifecycle_manager(session: DbSessionDep) -> ConnectionLifecycleManager:
    return ConnectionLifecycleManager(
        session,
        secret_provider=get_secret_provider(),
        pool_registry=get_pool_registry(),
        connector=_connector,
    )


ConnectionLifecycleManagerDep = Annotated[
    ConnectionLifecycleManager, Depends(get_connection_lifecycle_manager)
]


def get_schema_discovery_service(
    session: DbSessionDep, manager: ConnectionLifecycleManagerDep
) -> SchemaDiscoveryService:
    return SchemaDiscoveryService(session, connection_manager=manager)


SchemaDiscoveryServiceDep = Annotated[SchemaDiscoveryService, Depends(get_schema_discovery_service)]
