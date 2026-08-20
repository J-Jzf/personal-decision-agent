"""FastAPI 应用工厂模块。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings
from app.container import build_services


def create_app(settings: Settings | None = None) -> FastAPI:
    services = build_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await services.gateway.discover()
        yield
        await services.gateway.close()

    app = FastAPI(title="Personal Decision Agent", version="1.0.0", lifespan=lifespan)
    app.state.services = services
    app.include_router(router)
    return app


app = create_app()
