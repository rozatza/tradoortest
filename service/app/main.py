"""FastAPI skeleton. Health only; no trading routes."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    application = FastAPI(title="tradoortest", version="0.1.0")

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
