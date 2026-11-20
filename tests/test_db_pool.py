"""Pool configuration and send-concurrency limit."""
import asyncio

import pytest
from sqlalchemy.pool import NullPool

from app import db_pool

ENV = [
    "QUICKLY_DB_POOL_SIZE", "QUICKLY_DB_MAX_OVERFLOW", "QUICKLY_DB_POOL_TIMEOUT",
    "QUICKLY_DB_POOL_RECYCLE", "QUICKLY_DB_NULLPOOL", "QUICKLY_DB_JOBSTORE_POOL_SIZE",
    "QUICKLY_DB_JOBSTORE_MAX_OVERFLOW", "QUICKLY_SEND_CONCURRENCY",
]
SESSION = "postgresql+asyncpg://u:p@aws-0-eu.pooler.supabase.com:5432/postgres"
TXN = "postgresql+asyncpg://u:p@aws-0-eu.pooler.supabase.com:6543/postgres"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_defaults_are_small():
    kw = db_pool.async_engine_kwargs(SESSION)
    assert kw["pool_size"] == 3 and kw["max_overflow"] == 2
    assert kw["pool_pre_ping"] is True
    assert kw["pool_recycle"] == 300 and kw["pool_timeout"] == 30
    assert "connect_args" not in kw
    # default budget stays far below a 15-client pooler
    assert kw["pool_size"] + kw["max_overflow"] <= 5


def test_env_override_and_garbage(monkeypatch):
    monkeypatch.setenv("QUICKLY_DB_POOL_SIZE", "7")
    monkeypatch.setenv("QUICKLY_DB_MAX_OVERFLOW", "abc")
    monkeypatch.setenv("QUICKLY_DB_POOL_RECYCLE", "60")
    kw = db_pool.async_engine_kwargs(SESSION)
    assert kw["pool_size"] == 7
    assert kw["max_overflow"] == 2  # invalid value -> default
    assert kw["pool_recycle"] == 60


def test_pool_size_never_below_one(monkeypatch):
    monkeypatch.setenv("QUICKLY_DB_POOL_SIZE", "0")
    assert db_pool.async_engine_kwargs(SESSION)["pool_size"] == 1


def test_nullpool(monkeypatch):
    monkeypatch.setenv("QUICKLY_DB_NULLPOOL", "true")
    kw = db_pool.async_engine_kwargs(SESSION)
    assert kw["poolclass"] is NullPool and "pool_size" not in kw
    assert db_pool.jobstore_engine_kwargs()["poolclass"] is NullPool


def test_transaction_pooler_disables_prepared_statements():
    kw = db_pool.async_engine_kwargs(TXN)
    ca = kw["connect_args"]
    assert ca["statement_cache_size"] == 0
    assert ca["prepared_statement_cache_size"] == 0
    assert ca["prepared_statement_name_func"]() != ca["prepared_statement_name_func"]()


def test_jobstore_pool_is_tiny():
    kw = db_pool.jobstore_engine_kwargs()
    assert kw["pool_size"] == 1 and kw["max_overflow"] == 1


def test_build_jobstores_passes_engine_options():
    pytest.importorskip("psycopg2")
    from app.scheduler import build_jobstores

    stores = build_jobstores("postgresql+asyncpg://u:p@localhost:5432/x")
    eng = stores["default"].engine
    assert eng.pool.size() == 1
    assert eng.pool._max_overflow == 1


def test_engine_for_sqlite_unchanged():
    from app.database import engine

    assert engine.url.get_backend_name() == "sqlite"


def test_send_concurrency_default_and_env(monkeypatch):
    assert db_pool.send_concurrency() == 3
    monkeypatch.setenv("QUICKLY_SEND_CONCURRENCY", "1")
    assert db_pool.send_concurrency() == 1


@pytest.mark.asyncio
async def test_dispatch_limits_parallel_sends_and_releases(monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "_send_semaphore", asyncio.Semaphore(2))
    running = peak = 0

    async def fake_send(slot_id):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        if slot_id == 3:
            raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "send_slot_job", fake_send)
    jobs._pending_slot_ids.update(range(10))
    await asyncio.gather(*(jobs._dispatch_slot(i, 0) for i in range(10)))
    assert peak == 2
    assert running == 0
    assert not (set(range(10)) & jobs._pending_slot_ids)
    # exception in one slot released its permit: the semaphore is whole again
    assert jobs._send_semaphore._value == 2
