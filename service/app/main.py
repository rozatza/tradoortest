"""FastAPI app. Health, backtest jobs, saved strategies, tools, and the author agent."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.routes import router as agent_router
from app.jobs.queue import JobQueue
from app.jobs.routes import error_response, router
from app.ohlcv.routes import router as ohlcv_router
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
) -> FastAPI:
    application = FastAPI(title="tradoortest", version="0.1.0")
    application.state.jobs = jobs
    application.state.ohlcv = ohlcv
    application.state.strategies = strategies
    application.state.agent_store = agent_store
    application.state.llm_provider = llm_provider
    application.include_router(router)
    application.include_router(ohlcv_router)
    application.include_router(strategies_router)
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
