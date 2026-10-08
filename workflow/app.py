"""Builds the FastAPI app and wires its dependencies.

Run with `make run`, then open http://127.0.0.1:8000/docs.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workflow.api import router
from workflow.engine import WorkflowEngine
from workflow.logs import configure_logging, log_requests
from workflow.settings import Settings
from workflow.store import WorkflowStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = WorkflowEngine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        engine.start_workers()  # Workers for automatic tasks run while the app runs.
        yield
        await engine.stop_workers()

    app = FastAPI(title="Workflow Engine", lifespan=lifespan)
    app.middleware("http")(log_requests)
    app.state.engine = engine
    app.state.store = WorkflowStore()
    app.include_router(router)
    return app
