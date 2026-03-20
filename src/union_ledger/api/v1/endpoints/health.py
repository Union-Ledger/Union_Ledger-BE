from fastapi import APIRouter
from pydantic import BaseModel

from union_ledger.core.config import get_settings

router = APIRouter(prefix="/health", tags=["health"])


class HealthCheckResponse(BaseModel):
    service: str
    environment: str
    status: str


@router.get("", response_model=HealthCheckResponse)
async def get_health() -> HealthCheckResponse:
    settings = get_settings()
    return HealthCheckResponse(
        service=settings.app_name,
        environment=settings.environment,
        status="ok",
    )

