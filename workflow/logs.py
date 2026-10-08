"""Logging with the standard library: one format for the app and uvicorn, plus a request log.

Every line written while a request runs is tagged with its request id. Jobs started during
the request keep the id, so an automatic task's logs point back to the request that started it.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from uuid import uuid4

from fastapi import Request, Response

logger = logging.getLogger(__name__)

# The id lives in a context variable. A job captures the context when it is queued and runs in
# it, which is how an automatic task's logs keep the id of the request that started it.
request_id: ContextVar[str] = ContextVar("request_id", default="-")


class _AddRequestId(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_AddRequestId())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Send uvicorn's logs through the handler above. Its access log is off, because
    # `log_requests` logs each request with more detail.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True


async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log one line per request, and tag every line written during it with a request id."""
    rid = request.headers.get("X-Request-ID") or uuid4().hex[:8]
    token = request_id.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        logger.info(
            "%s %s → %d in %.1f ms (user %s)",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request.headers.get("X-User", "-"),
        )
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        request_id.reset(token)
