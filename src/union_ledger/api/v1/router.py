from fastapi import APIRouter

from union_ledger.api.v1.endpoints.auth import router as auth_router
from union_ledger.api.v1.endpoints.evidences import router as evidences_router
from union_ledger.api.v1.endpoints.health import router as health_router
from union_ledger.api.v1.endpoints.system import router as system_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(health_router)
router.include_router(system_router)
router.include_router(evidences_router)
