"""Central SQLAlchemy engine factory for the shared SQLite database.

Every pipeline obtains its engine here so that *all* connections — across
all parallel agent worktrees and the live scrape — open the one shared
database with the same concurrency-safe PRAGMAs.

Why this matters: multiple processes (the Martindale scrape, website
enrichment, canonical resolution) read and write the single
``data/legal_sourcing.sqlite`` file concurrently. SQLite WAL mode allows many
concurrent readers plus a single writer, but writers still serialize. The
``busy_timeout`` PRAGMA is the single most important setting for that
arrangement: without it, a connection that loses the writer race fails
*instantly* with ``database is locked`` instead of waiting its turn. It was
previously set (via ``connect_args={"timeout": 30}``) in only three of the
scrapers; resolution, website enrichment and several others fell back to
sqlite's ~5 s default. Routing everything through :func:`make_engine`
guarantees a uniform 30 s timeout (and re-asserts WAL) on every connection.

See docs/assumptions.md — "Multi-agent shared database" (2026-06-04).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from legal_sourcing.config import get_settings

#: Block up to this long for the single-writer lock before raising
#: ``OperationalError: database is locked``. 30 s matches the value the
#: high-volume scrapers already used and is far above sqlite's ~5 s default.
BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Set per-connection PRAGMAs every time the pool opens a connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # WAL is persisted in the DB header, but assert it so no connection
        # is ever the one to silently leave the file in rollback-journal mode
        # (which would make writers block readers).
        cursor.execute("PRAGMA journal_mode = WAL")
    finally:
        cursor.close()


def make_engine(**kwargs: Any) -> Engine:
    """Create an :class:`~sqlalchemy.engine.Engine` for the shared database.

    All pipelines should use this instead of calling ``create_engine``
    directly so that every connection gets the concurrency-safe PRAGMAs above.
    Extra keyword arguments are forwarded to ``create_engine`` (e.g.
    ``echo=True``); a caller-supplied ``connect_args`` is merged with the
    mandatory ``timeout``.
    """
    settings = get_settings()
    connect_args = {"timeout": BUSY_TIMEOUT_MS / 1000}
    connect_args.update(kwargs.pop("connect_args", {}) or {})
    engine = create_engine(settings.db_url, connect_args=connect_args, **kwargs)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine
