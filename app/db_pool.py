"""Connection-pool configuration shared by the async engine and the job store.

Quickly may run against a pooler with a hard client limit (for example the
Supabase session pooler: 15 clients).  SQLAlchemy's defaults (pool_size 5 +
max_overflow 10 = 15 per engine) would then fill the limit with a single
engine, so the pools are small by default and tunable via environment:

  QUICKLY_DB_POOL_SIZE      persistent connections per engine   (default 3)
  QUICKLY_DB_MAX_OVERFLOW   extra burst connections             (default 2)
  QUICKLY_DB_POOL_TIMEOUT   seconds to wait for a connection    (default 30)
  QUICKLY_DB_POOL_RECYCLE   recycle connections after seconds   (default 300)
  QUICKLY_DB_NULLPOOL       true -> no pooling, connect per use (default false)
  QUICKLY_DB_JOBSTORE_POOL_SIZE / _MAX_OVERFLOW  APScheduler store (1 / 1)
  QUICKLY_SEND_CONCURRENCY  max slots sending at the same time  (default 3)
"""
from __future__ import annotations

import os
import uuid

from sqlalchemy import make_url
from sqlalchemy.pool import NullPool

DEFAULT_POOL_SIZE = 3
DEFAULT_MAX_OVERFLOW = 2
DEFAULT_POOL_TIMEOUT = 30
DEFAULT_POOL_RECYCLE = 300
DEFAULT_JOBSTORE_POOL_SIZE = 1
DEFAULT_JOBSTORE_MAX_OVERFLOW = 1
DEFAULT_SEND_CONCURRENCY = 3


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def is_transaction_pooler(db_url: str) -> bool:
    """Port 6543 is the Supabase/Supavisor transaction-mode pooler."""
    try:
        return make_url(db_url).port == 6543
    except Exception:
        return False


def async_engine_kwargs(db_url: str) -> dict:
    """Pool kwargs (+ asyncpg connect_args) for ``create_async_engine``."""
    kwargs: dict = {"pool_pre_ping": True}
    if env_bool("QUICKLY_DB_NULLPOOL"):
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=env_int("QUICKLY_DB_POOL_SIZE", DEFAULT_POOL_SIZE, 1),
            max_overflow=env_int("QUICKLY_DB_MAX_OVERFLOW", DEFAULT_MAX_OVERFLOW, 0),
            pool_timeout=env_int("QUICKLY_DB_POOL_TIMEOUT", DEFAULT_POOL_TIMEOUT, 1),
            pool_recycle=env_int("QUICKLY_DB_POOL_RECYCLE", DEFAULT_POOL_RECYCLE, 1),
        )
    if is_transaction_pooler(db_url):
        # Transaction mode hands every statement to a different server
        # connection: prepared statements must not be cached or named.
        kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    return kwargs


def jobstore_engine_kwargs() -> dict:
    """Pool kwargs for the synchronous (psycopg2) APScheduler job-store engine."""
    if env_bool("QUICKLY_DB_NULLPOOL"):
        return {"poolclass": NullPool, "pool_pre_ping": True}
    return {
        "pool_size": env_int("QUICKLY_DB_JOBSTORE_POOL_SIZE", DEFAULT_JOBSTORE_POOL_SIZE, 1),
        "max_overflow": env_int("QUICKLY_DB_JOBSTORE_MAX_OVERFLOW", DEFAULT_JOBSTORE_MAX_OVERFLOW, 0),
        "pool_pre_ping": True,
        "pool_recycle": env_int("QUICKLY_DB_POOL_RECYCLE", DEFAULT_POOL_RECYCLE, 1),
        "pool_timeout": env_int("QUICKLY_DB_POOL_TIMEOUT", DEFAULT_POOL_TIMEOUT, 1),
    }


def send_concurrency() -> int:
    return env_int("QUICKLY_SEND_CONCURRENCY", DEFAULT_SEND_CONCURRENCY, 1)
