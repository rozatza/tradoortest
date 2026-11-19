"""FastAPI app. Health, and the single-user backtest job API."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.jobs.queue import JobQueue
from app.jobs.routes import error_response, router
from app.ohlcv.routes import router as ohlcv_router


def create_app(*, jobs: JobQueue | None = None, ohlcv: object | None = None) -> FastAPI:
    application = FastAPI(title="tradoortest", version="0.1.0")
    application.state.jobs = jobs
    application.state.ohlcv = ohlcv
    application.include_router(router)
    application.include_router(ohlcv_router)

    @application.exception_handler(RecursionError)
    def recursion_too_deep(_request: Request, _exc: RecursionError) -> JSONResponse:
        return error_response(422, "expr_too_deep", "expression nested too deeply", None)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
