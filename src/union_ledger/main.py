from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from union_ledger import __version__
from union_ledger.api.router import api_router
from union_ledger.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", tags=["root"])
    async def read_root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "status": "ok",
            "docs": "/docs",
            "openapi": f"{settings.api_v1_prefix}/openapi.json",
        }

    return app


app = create_app()

