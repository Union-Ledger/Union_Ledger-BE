from fastapi import APIRouter

from union_ledger.api.v1.endpoints.audit import router as audit_router
from union_ledger.api.v1.endpoints.auth import router as auth_router
from union_ledger.api.v1.endpoints.bank_statements import router as bank_statements_router
from union_ledger.api.v1.endpoints.evidences import router as evidences_router
from union_ledger.api.v1.endpoints.health import router as health_router
from union_ledger.api.v1.endpoints.invitations import router as invitations_router
from union_ledger.api.v1.endpoints.notifications import router as notifications_router
from union_ledger.api.v1.endpoints.organizations import router as organizations_router
from union_ledger.api.v1.endpoints.reconciliation import router as reconciliation_router
from union_ledger.api.v1.endpoints.settlement_templates import (
    router as settlement_templates_router,
)
from union_ledger.api.v1.endpoints.settlements import router as settlements_router
from union_ledger.api.v1.endpoints.system import router as system_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(health_router)
router.include_router(system_router)
router.include_router(organizations_router)
router.include_router(invitations_router)
router.include_router(settlement_templates_router)
router.include_router(settlements_router)
router.include_router(audit_router)
router.include_router(evidences_router)
router.include_router(bank_statements_router)
router.include_router(reconciliation_router)
router.include_router(notifications_router)
