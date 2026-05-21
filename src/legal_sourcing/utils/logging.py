"""Structured logging setup.

Call `configure_logging()` once at process startup (CLI entry points), then
use `get_logger(__name__)` everywhere else.

Logs render as key=value pairs to stderr by default — human-readable in dev,
trivially parseable in prod. Swap the renderer for JSON when we ship.
"""

from __future__ import annotations

import logging
import sys

import structlog

from legal_sourcing.config import get_settings

_configured = False


def configure_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return

    settings = get_settings()
    log_level = (level or settings.log_level).upper()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
