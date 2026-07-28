"""Customer database connection endpoints.

Every response schema here is built to be safe by construction: `ConnectionResponse` has no
field that could carry a secret (Phase 4 requirement — "never return resolved secrets through
an API," "delete connection metadata without exposing the secret"). Registration and secret
rotation accept a secret value in the *request* body (there is no other way to give the
platform a credential), but it is never echoed back in the response.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from apps.api.deps import ConnectionLifecycleManagerDep, DbSessionDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.connectors.enums import ConnectionStatus, DatabaseEngine
from hermes_rpt.connectors.models import CustomerDatabaseConnection
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/connections", tags=["connections"])

_ManageScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.CONNECTION_MANAGE))]


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    engine: DatabaseEngine
    host: str
    port: int
    database_name: str
    username: str
    tls_mode: str
    status: ConnectionStatus
    schema_allowlist: list[str]
    table_allowlist: list[str]
    last_validated_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_connection(cls, connection: CustomerDatabaseConnection) -> ConnectionResponse:
        return cls(
            id=connection.id,
            tenant_id=connection.tenant_id,
            name=connection.name,
            engine=connection.engine,
            host=connection.host,
            port=connection.port,
            database_name=connection.database_name,
            username=connection.username,
            tls_mode=connection.tls_mode,
            status=connection.status,
            schema_allowlist=connection.schema_allowlist,
            table_allowlist=connection.table_allowlist,
            last_validated_at=connection.last_validated_at,
            last_error=connection.last_error,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )


class ConnectionRegisterRequest(BaseModel):
    name: str
    engine: DatabaseEngine = DatabaseEngine.POSTGRESQL
    host: str
    port: int = 5432
    database_name: str
    username: str
    secret_value: str = Field(description="The database password. Never stored as-is or returned.")
    tls_mode: str = "require"
    schema_allowlist: list[str] = Field(default_factory=list)
    table_allowlist: list[str] = Field(default_factory=list)


class RotateSecretRequest(BaseModel):
    new_secret_value: str


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def register_connection(
    body: ConnectionRegisterRequest,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ConnectionResponse:
    connection = await manager.register_connection(
        tenant_context=tenant_context,
        name=body.name,
        engine=body.engine,
        host=body.host,
        port=body.port,
        database_name=body.database_name,
        username=body.username,
        secret_value=body.secret_value,
        tls_mode=body.tls_mode,
        schema_allowlist=body.schema_allowlist,
        table_allowlist=body.table_allowlist,
    )
    await session.commit()
    return ConnectionResponse.from_orm_connection(connection)


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    manager: ConnectionLifecycleManagerDep, tenant_context: _ManageScope
) -> list[ConnectionResponse]:
    connections = await manager.list_connections(tenant_context=tenant_context)
    return [ConnectionResponse.from_orm_connection(c) for c in connections]


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: uuid.UUID, manager: ConnectionLifecycleManagerDep, tenant_context: _ManageScope
) -> ConnectionResponse:
    connection = await manager.get_connection(connection_id, tenant_context=tenant_context)
    return ConnectionResponse.from_orm_connection(connection)


@router.post("/{connection_id}/validate", response_model=ConnectionResponse)
async def validate_connection(
    connection_id: uuid.UUID,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ConnectionResponse:
    connection = await manager.validate_connection(connection_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(connection)
    return ConnectionResponse.from_orm_connection(connection)


@router.post("/{connection_id}/enable", response_model=ConnectionResponse)
async def enable_connection(
    connection_id: uuid.UUID,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ConnectionResponse:
    connection = await manager.enable_connection(connection_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(connection)
    return ConnectionResponse.from_orm_connection(connection)


@router.post("/{connection_id}/disable", response_model=ConnectionResponse)
async def disable_connection(
    connection_id: uuid.UUID,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ConnectionResponse:
    connection = await manager.disable_connection(connection_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(connection)
    return ConnectionResponse.from_orm_connection(connection)


@router.post("/{connection_id}/rotate-secret", response_model=ConnectionResponse)
async def rotate_secret(
    connection_id: uuid.UUID,
    body: RotateSecretRequest,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ConnectionResponse:
    connection = await manager.rotate_secret(
        connection_id, tenant_context=tenant_context, new_secret_value=body.new_secret_value
    )
    await session.commit()
    await session.refresh(connection)
    return ConnectionResponse.from_orm_connection(connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    manager: ConnectionLifecycleManagerDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> None:
    await manager.delete_connection(connection_id, tenant_context=tenant_context)
    await session.commit()
