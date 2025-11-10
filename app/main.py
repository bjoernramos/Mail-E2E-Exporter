from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .logging_setup import logger
from .config import APP_VERSION
from .routes import router
from .runner import start_background, stop_background

app = FastAPI(title="Mail E2E Exporter", version=APP_VERSION)
app.include_router(router)


@app.on_event("startup")
async def on_startup():
    start_background()


@app.on_event("shutdown")
async def on_shutdown():
    stop_background()


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)})
