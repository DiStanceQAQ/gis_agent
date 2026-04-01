from __future__ import annotations

from contextvars import ContextVar, Token
import logging


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    root_logger = logging.getLogger()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(request_id)s] %(name)s - %(message)s"
    )

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(existing_filter, RequestIdFilter) for existing_filter in handler.filters):
                handler.addFilter(RequestIdFilter())

    root_logger.setLevel(level)
    logging.getLogger("python_multipart").setLevel(logging.INFO)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


def bind_request_id(request_id: str) -> Token[str]:
    return request_id_var.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    request_id_var.reset(token)


def get_request_id() -> str:
    return request_id_var.get()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
