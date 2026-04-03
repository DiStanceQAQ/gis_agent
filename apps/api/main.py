from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api.routers import artifacts, files, health, messages, sessions, tasks
from packages.domain.config import get_settings
from packages.domain.errors import AppError, ErrorCode, build_error_response, normalize_validation_errors
from packages.domain.logging import (
    bind_request_id,
    configure_logging,
    get_logger,
    get_request_id,
    reset_request_id,
)
from packages.domain.migrations import run_migrations
from packages.domain.services.storage import ensure_storage_dirs


settings = get_settings()
configure_logging(settings.debug)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    ensure_storage_dirs(settings)
    run_migrations()
    yield


app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(sessions.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(messages.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(artifacts.router, prefix=settings.api_prefix)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    token = bind_request_id(request_id)
    request.state.request_id = request_id
    started_at = perf_counter()
    logger.info("request.started method=%s path=%s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((perf_counter() - started_at) * 1000)
        logger.exception(
            "request.failed method=%s path=%s status=500 duration_ms=%s",
            request.method,
            request.url.path,
            duration_ms,
        )
        reset_request_id(token)
        raise

    duration_ms = int((perf_counter() - started_at) * 1000)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request.completed method=%s path=%s status=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    reset_request_id(token)
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning(
        "app_error code=%s path=%s status=%s",
        exc.error_code,
        request.url.path,
        exc.status_code,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_response(
            request_id=getattr(request.state, "request_id", get_request_id()),
            error_code=exc.error_code,
            message=exc.message,
            detail=exc.detail,
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=build_error_response(
            request_id=getattr(request.state, "request_id", get_request_id()),
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed.",
            detail=normalize_validation_errors(exc.errors()),
        ),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    detail = None if isinstance(exc.detail, str) else exc.detail
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_response(
            request_id=getattr(request.state, "request_id", get_request_id()),
            error_code=ErrorCode.BAD_REQUEST,
            message=message,
            detail=detail,
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("internal_error path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content=build_error_response(
            request_id=getattr(request.state, "request_id", get_request_id()),
            error_code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error.",
        ),
    )
