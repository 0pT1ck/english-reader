"""Uniform error responses.

Every error the API returns carries four things: a stable machine-readable
``code``, a human sentence in Chinese, optional structured ``detail``, and the
``trace_id``.

The trace id is the important one. It closes the diagnostic loop described in
the design: a client shows the id when something fails, the user copies it, and
one query pulls the entire chain of what happened during that request —
including the DEBUG records that were flushed to disk precisely because an error
occurred. Without it, the user has to describe symptoms and the AI has to guess.

Error codes are part of the API contract (architecture rule 5: additive only).
Adding a code is fine; changing what an existing one means is not, because a
sideloaded client months out of date may still be branching on it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core.logging import current_trace, get_logger

log = get_logger("core.errors")


class AppError(Exception):
    """Base for errors this application raises deliberately.

    Anything raised as an ``AppError`` is a known, described failure mode and is
    logged at WARNING. Anything else reaching the handler is a bug and is logged
    at ERROR — which is also what triggers alerting and the DEBUG flush.
    """

    code = "internal_error"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "服务内部错误"

    def __init__(self, message: str | None = None, **detail: Any) -> None:
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)


class NotFound(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "找不到请求的资源"


class InvalidRequest(AppError):
    code = "invalid_request"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "请求参数不正确"


class Unauthorized(AppError):
    code = "unauthorized"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "未通过认证"


class Forbidden(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN
    message = "没有访问权限"


class NotReady(AppError):
    """A prerequisite has not been satisfied yet.

    Used for things like "the dictionary has not been imported" — not a bug, not
    a bad request, just work the user still has to do. Distinguished from other
    errors so the admin console can tell them apart and say what to do next.
    """

    code = "not_ready"
    status_code = status.HTTP_409_CONFLICT
    message = "前置条件尚未满足"


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "trace_id": current_trace(),
        }
    }
    if detail:
        body["error"]["detail"] = detail
    return body


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that give every failure the same shape."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "request.rejected",
            f"请求被拒绝：{exc.message}",
            code=exc.code,
            path=request.url.path,
            detail=exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        log.warning(
            "request.invalid",
            "请求参数校验失败",
            path=request.url.path,
            errors=exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body("validation_failed", "请求参数不符合接口要求", exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Mostly 404s for unknown paths. INFO rather than WARNING: a mistyped
        # URL is not an incident, and logging it louder would train the user to
        # ignore alerts.
        #
        # **It has to be logged at all, though**, and for a while it was not:
        # the comment above described the intent and no call implemented it. A
        # routing 404 went out carrying a trace_id and left nothing behind, so
        # handing that trace_id over — the whole point of putting it in the
        # response — turned up an empty result. Found on 2026-09-09 when a 404
        # from the review page could not be traced to a path.
        log.info(
            "request.http_error",
            f"HTTP {exc.status_code}：{exc.detail}",
            status=exc.status_code,
            path=request.url.path,
            method=request.method,
            query=str(request.url.query) or None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # A bug. ERROR level means: DEBUG records for this trace get flushed to
        # disk, and the alert hooks fire.
        log.exception(
            "request.unhandled_error",
            "请求处理中出现未预期的错误",
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "服务内部错误，请把上面的 trace_id 提供给排查方"),
        )
