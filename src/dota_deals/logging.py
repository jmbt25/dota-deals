"""structlog setup.

Output goes to stderr. ``log_format="console"`` renders human-readable output;
``log_format="json"`` renders one JSON object per line. Bind context with
``logger.bind(item_id=...)``.

The configuration uses ``structlog.contextvars`` so ``run_id`` (and any other
context bound at process scope) is carried into every async task without
threading it through every call.
"""

from __future__ import annotations

import sys
from typing import cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import Processor

from dota_deals.config import LogFormat


def configure_logging(run_id: str, log_format: LogFormat) -> None:
    """Configure structlog for the lifetime of the process.

    Idempotent — subsequent calls overwrite the previous configuration. Should
    be called once at the top of each CLI invocation, bound with the
    ``parent_run_id`` of that invocation.

    :param run_id: parent run identifier; bound into every log line via
        structlog's contextvars.
    :param log_format: ``"console"`` for human-readable, ``"json"`` for
        line-delimited JSON.
    """
    renderer: Processor
    if log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        renderer,
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)


def get_logger(name: str) -> BoundLogger:
    """Return a logger bound to ``name`` (typically the module ``__name__``).

    The returned logger inherits any context bound at process level via
    :func:`structlog.contextvars.bind_contextvars`. Callers should further bind
    per-entity context (``item_id``, ``source``) using ``logger.bind(...)``.
    """
    return cast(BoundLogger, structlog.get_logger(name))
