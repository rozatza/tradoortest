"""FastAPI app. Health, jobs, saved strategies, paper deploy, tools, and the author agent."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.routes import router as agent_router
from app.jobs.queue import JobQueue
from app.jobs.routes import error_response, router
from app.live.scheduler import PaperScheduler, live_feed_enabled
from app.ohlcv.routes import router as ohlcv_router
from app.paper.routes import router as paper_router
from app.strategies.routes import router as strategies_router
from app.strategies.store import StrategyStore
from app.tools.routes import router as tools_router


def create_app(
    *,
    jobs: JobQueue | None = None,
    ohlcv: object | None = None,
    strategies: StrategyStore | None = None,
    agent_store: object | None = None,
    llm_provider: object | None = None,
    live: PaperScheduler | None = None,
    live_enabled: bool | None = None,
) -> FastAPI:
    """Build the app. The live scheduler is off unless ``live`` or the env flag is set."""
    enabled = live_feed_enabled() if live_enabled is None else live_enabled
    scheduler = live
    if scheduler is None and enabled:
        from app.live.scheduler import open_scheduler

        scheduler = open_scheduler()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # The worker is a thread. Starting it does not run a tick on this loop.
        current = application.state.live_scheduler
        if current is not None:
            current.start()
        try:
            yield
        finally:
            if current is not None:
                current.stop()

    application = FastAPI(title="tradoortest", version="0.1.0", lifespan=lifespan)
    application.state.jobs = jobs
    application.state.ohlcv = ohlcv
    application.state.strategies = strategies
    application.state.agent_store = agent_store
    application.state.llm_provider = llm_provider
    application.state.live_scheduler = scheduler
    application.include_router(router)
    application.include_router(ohlcv_router)
    application.include_router(strategies_router)
    application.include_router(paper_router)
    application.include_router(tools_router)
    application.include_router(agent_router)

    @application.exception_handler(RecursionError)
    def recursion_too_deep(_request: Request, _exc: RecursionError) -> JSONResponse:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
