"""structlog setup.

Output goes to stderr. ``log_format="console"`` renders human-readable output;
``log_format="json"`` renders one JSON object per line. Bind context with
``logger.bind(item_id=...)``.
"""

from __future__ import annotations

from structlog.stdlib import BoundLogger

from dota_deals.config import LogFormat


def configure_logging(run_id: str, log_format: LogFormat) -> None:
    """Configure structlog for the lifetime of the process.

    Idempotent — subsequent calls overwrite the previous configuration. Should
    be called once at the top of each CLI invocation, bound with the
    ``parent_run_id`` of that invocation.

    :param run_id: parent run identifier; bound into every log line.
    :param log_format: ``"console"`` for human-readable, ``"json"`` for
        line-delimited JSON.
    """
    raise NotImplementedError


def get_logger(name: str) -> BoundLogger:
    """Return a logger bound to ``name`` (typically the module ``__name__``).

    The returned logger inherits any context bound at process level. Callers
    should further bind per-entity context (``item_id``, ``source``) using
    ``logger.bind(...)``.
    """
    raise NotImplementedError
