from fastapi import APIRouter
from pydantic import BaseModel

from union_ledger import __version__
from union_ledger.core.config import get_settings

router = APIRouter(prefix="/system", tags=["system"])


class SystemMetadataResponse(BaseModel):
    service: str
    version: str
    environment: str
    api_prefix: str


@router.get("/metadata", response_model=SystemMetadataResponse)
async def get_system_metadata() -> SystemMetadataResponse:
    settings = get_settings()
    return SystemMetadataResponse(
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        api_prefix=settings.api_v1_prefix,
    )

