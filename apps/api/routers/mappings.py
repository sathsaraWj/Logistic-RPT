"""Schema mapping endpoints (Phase 7): lifecycle management plus a suggestion endpoint that
runs the deterministic engine (`hermes_rpt.mappings.suggest`) against an already-discovered
snapshot without persisting anything — callers review the suggestion and then `POST` it (or
their own hand-edited document) as a new draft.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from apps.api.deps import DbSessionDep
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.mappings.document import MappingDocument
from hermes_rpt.mappings.enums import MappingState
from hermes_rpt.mappings.models import SchemaMapping
from hermes_rpt.mappings.repository import SchemaMappingRepository
from hermes_rpt.mappings.service import MappingService
from hermes_rpt.mappings.suggest import suggest_mapping
from hermes_rpt.ontology.registry import get_ontology
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult
from hermes_rpt.schemas.repository import SchemaSnapshotRepository
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/mappings", tags=["mappings"])

_ManageScope = Annotated[TenantContext, Depends(require_scopes(ScopeName.MAPPING_MANAGE))]


class MappingResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    entity_name: str
    schema_snapshot_id: uuid.UUID
    ontology_version: str
    state: MappingState
    active_version_id: uuid.UUID | None
    suspended_due_to_drift: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_mapping(cls, mapping: SchemaMapping) -> MappingResponse:
        return cls(
            id=mapping.id,
            tenant_id=mapping.tenant_id,
            entity_name=mapping.entity_name,
            schema_snapshot_id=mapping.schema_snapshot_id,
            ontology_version=mapping.ontology_version,
            state=mapping.state,
            active_version_id=mapping.active_version_id,
            suspended_due_to_drift=mapping.suspended_due_to_drift,
            created_at=mapping.created_at,
            updated_at=mapping.updated_at,
        )


class CreateDraftRequest(BaseModel):
    schema_snapshot_id: uuid.UUID
    document: dict[str, Any]
    ontology_version: str = "1"


class SuggestionResponse(BaseModel):
    document: dict[str, Any]
    confidence: float
    explanation: str


@router.get(
    "/suggest/{schema_snapshot_id}/{entity_name}/{schema_name}/{table_name}",
    response_model=SuggestionResponse,
)
async def suggest_mapping_endpoint(
    schema_snapshot_id: uuid.UUID,
    entity_name: str,
    schema_name: str,
    table_name: str,
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.SCHEMA_DISCOVER))],
) -> SuggestionResponse:
    snapshot = await SchemaSnapshotRepository(session).require(
        schema_snapshot_id, tenant_context=tenant_context
    )
    introspection = SchemaIntrospectionResult.model_validate(snapshot.metadata_document)
    table = next(
        (t for t in introspection.tables if t.schema_name == schema_name and t.name == table_name),
        None,
    )
    if table is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Table not found in this snapshot")

    try:
        ontology_entity = get_ontology().get_entity(entity_name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown ontology entity") from exc

    try:
        result = suggest_mapping(ontology_entity=ontology_entity, table=table)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return SuggestionResponse(
        document=result.document.model_dump(mode="json", by_alias=True),
        confidence=result.confidence,
        explanation=result.explanation,
    )


@router.post("", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: CreateDraftRequest, session: DbSessionDep, tenant_context: _ManageScope
) -> MappingResponse:
    document = MappingDocument.model_validate(body.document)
    mapping, _version = await MappingService(session).create_draft(
        tenant_context=tenant_context,
        schema_snapshot_id=body.schema_snapshot_id,
        document=document,
        ontology_version=body.ontology_version,
    )
    await session.commit()
    return MappingResponse.from_orm_mapping(mapping)


@router.get("", response_model=list[MappingResponse])
async def list_mappings(
    session: DbSessionDep, tenant_context: _ManageScope
) -> list[MappingResponse]:
    mappings = await SchemaMappingRepository(session).list_for_tenant(tenant_context=tenant_context)
    return [MappingResponse.from_orm_mapping(m) for m in mappings]


@router.get("/{mapping_id}", response_model=MappingResponse)
async def get_mapping(
    mapping_id: uuid.UUID, session: DbSessionDep, tenant_context: _ManageScope
) -> MappingResponse:
    mapping = await SchemaMappingRepository(session).require(
        mapping_id, tenant_context=tenant_context
    )
    return MappingResponse.from_orm_mapping(mapping)


@router.post("/{mapping_id}/submit-for-validation", response_model=MappingResponse)
async def submit_for_validation(
    mapping_id: uuid.UUID, session: DbSessionDep, tenant_context: _ManageScope
) -> MappingResponse:
    mapping = await MappingService(session).submit_for_validation(
        mapping_id, tenant_context=tenant_context
    )
    await session.commit()
    await session.refresh(mapping)
    return MappingResponse.from_orm_mapping(mapping)


@router.post("/{mapping_id}/approve", response_model=MappingResponse)
async def approve(
    mapping_id: uuid.UUID,
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.TENANT_ADMIN))],
) -> MappingResponse:
    mapping = await MappingService(session).approve(mapping_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(mapping)
    return MappingResponse.from_orm_mapping(mapping)


@router.post("/{mapping_id}/activate", response_model=MappingResponse)
async def activate(
    mapping_id: uuid.UUID,
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.TENANT_ADMIN))],
) -> MappingResponse:
    mapping = await MappingService(session).activate(mapping_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(mapping)
    return MappingResponse.from_orm_mapping(mapping)


@router.post("/{mapping_id}/deprecate", response_model=MappingResponse)
async def deprecate(
    mapping_id: uuid.UUID, session: DbSessionDep, tenant_context: _ManageScope
) -> MappingResponse:
    mapping = await MappingService(session).deprecate(mapping_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(mapping)
    return MappingResponse.from_orm_mapping(mapping)


@router.post("/{mapping_id}/reject", response_model=MappingResponse)
async def reject(
    mapping_id: uuid.UUID, session: DbSessionDep, tenant_context: _ManageScope
) -> MappingResponse:
    mapping = await MappingService(session).reject(mapping_id, tenant_context=tenant_context)
    await session.commit()
    await session.refresh(mapping)
    return MappingResponse.from_orm_mapping(mapping)
