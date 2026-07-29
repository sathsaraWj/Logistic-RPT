"""Import every ORM model module so `Base.metadata` is fully populated.

SQLAlchemy only registers a class against `Base.metadata` when its module has actually been
imported. Alembic autogenerate and `Base.metadata.create_all()` (used by tests) both need every
entity registered first — importing this module once, before either, is the one place that
guarantees it rather than relying on import order elsewhere.
"""

from __future__ import annotations

from hermes_rpt.audit import models as _audit_models
from hermes_rpt.auth import models as _auth_models
from hermes_rpt.connectors import models as _connectors_models
from hermes_rpt.inference import models as _inference_models
from hermes_rpt.mappings import models as _mappings_models
from hermes_rpt.registry import models as _registry_models
from hermes_rpt.schemas import models as _schemas_models
from hermes_rpt.tenants import models as _tenants_models

__all__ = [
    "_audit_models",
    "_auth_models",
    "_connectors_models",
    "_inference_models",
    "_mappings_models",
    "_registry_models",
    "_schemas_models",
    "_tenants_models",
]
