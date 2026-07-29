"""Shared, non-auth FastAPI dependencies for apps/api routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.auth.service import ServiceCredentialService
from hermes_rpt.common.db import get_session
from hermes_rpt.common.settings import get_settings
from hermes_rpt.connectors.pool_registry import get_pool_registry
from hermes_rpt.connectors.postgres import PostgresConnector
from hermes_rpt.connectors.service import ConnectionLifecycleManager
from hermes_rpt.features.service import FeatureExtractionService
from hermes_rpt.inference.model_loading import ModelLoader
from hermes_rpt.inference.service import PredictionService
from hermes_rpt.monitoring.outcomes import OutcomeService
from hermes_rpt.monitoring.service import MonitoringService
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


def get_feature_extraction_service(
    session: DbSessionDep, manager: ConnectionLifecycleManagerDep
) -> FeatureExtractionService:
    return FeatureExtractionService(session, connection_manager=manager)


FeatureExtractionServiceDep = Annotated[
    FeatureExtractionService, Depends(get_feature_extraction_service)
]


@lru_cache
def get_model_loader() -> ModelLoader:
    """Process-wide singleton — the in-process model cache and circuit breaker
    (`hermes_rpt.inference.model_loading.ModelLoader`) are meaningless if rebuilt per request."""

    return ModelLoader(mlflow_tracking_uri=get_settings().mlflow_tracking_uri)


ModelLoaderDep = Annotated[ModelLoader, Depends(get_model_loader)]


def get_prediction_service(
    session: DbSessionDep, manager: ConnectionLifecycleManagerDep, model_loader: ModelLoaderDep
) -> PredictionService:
    return PredictionService(session, connection_manager=manager, model_loader=model_loader)


PredictionServiceDep = Annotated[PredictionService, Depends(get_prediction_service)]


def get_monitoring_service(session: DbSessionDep) -> MonitoringService:
    return MonitoringService(session)


MonitoringServiceDep = Annotated[MonitoringService, Depends(get_monitoring_service)]


def get_outcome_service(session: DbSessionDep) -> OutcomeService:
    return OutcomeService(session)


OutcomeServiceDep = Annotated[OutcomeService, Depends(get_outcome_service)]


def get_service_credential_service(session: DbSessionDep) -> ServiceCredentialService:
    return ServiceCredentialService(session, settings=get_settings())


ServiceCredentialServiceDep = Annotated[
    ServiceCredentialService, Depends(get_service_credential_service)
]
