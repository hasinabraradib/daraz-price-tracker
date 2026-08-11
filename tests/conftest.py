"""Test setup notes:

- `api/app` and `worker/worker_app` are separate top-level packages (the
  worker's was renamed from `app` to `worker_app` specifically so both can
  be imported in the same pytest process without colliding — see the
  README's "Tests" section for why). We add repo root, `api/`, and
  `worker/` to sys.path below so `shared`, `app`, and `worker_app` are all
  importable as distinct packages.
- Tests never touch the dev database or a real Redis. DATABASE_URL is
  overridden to a dedicated `..._test` database (created fresh and dropped
  at the end of the session) *before* any app module that reads it at
  import time gets imported. Redis is replaced per-test with fakeredis via
  the `fake_redis` fixture (autouse, so no test can accidentally reach a
  real Redis).
- No test in this suite calls the real Playwright/scraper network path —
  scraper-facing tests mock `worker_app.scraper`'s Playwright calls
  directly.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "api", REPO_ROOT / "worker"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _with_db_name(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", "", ""))


_BASE_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://daraz:daraz@localhost:5432/daraz_price_tracker"
)
_TEST_DB_NAME = "daraz_price_tracker_test"
_TEST_DATABASE_URL = _with_db_name(_BASE_DATABASE_URL, _TEST_DB_NAME)
_ADMIN_DATABASE_URL = _with_db_name(_BASE_DATABASE_URL, "postgres")

# Must happen before the first import of shared.config/shared.database
# (which read these at module-import time) anywhere in the test session.
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")  # unused (fakeredis)
os.environ["SQLALCHEMY_NULL_POOL"] = "1"

import fakeredis.aioredis  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import shared.queue as queue_module  # noqa: E402
from shared.database import Base, async_session_factory, engine  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _test_database():
    """Create a dedicated test database for the session, build the schema
    from the current models (no Alembic here — migrations are verified
    separately; tests just need the schema to match `shared/models.py`),
    and drop the database again once the whole suite finishes."""
    admin_engine = create_async_engine(_ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{_TEST_DB_NAME}"'))
    await admin_engine.dispose()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await engine.dispose()

    admin_engine = create_async_engine(_ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}" WITH (FORCE)'))
    await admin_engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate every app table before each test. The app code commits
    directly (no injectable outer transaction to roll back), so
    truncate-before is the simplest reliable isolation between tests."""
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE products, price_snapshots, scrape_attempts RESTART IDENTITY CASCADE")
        )
    yield


@pytest_asyncio.fixture
async def db_session():
    session = async_session_factory()
    try:
        yield session
    finally:
        # Closing triggers an implicit rollback of whatever's uncommitted.
        # With NullPool + several event loops in play across a test (the
        # ASGI app's own get_db() sessions, this fixture's session, etc.),
        # that cleanup occasionally races a loop that's already gone by
        # teardown time — harmless (NullPool means the connection was
        # never going to be reused anyway), so don't fail the test over it.
        try:
            await session.close()
        except RuntimeError:
            pass


@pytest_asyncio.fixture(autouse=True)
async def fake_redis(monkeypatch):
    """Replace shared.queue's module-level Redis client with fakeredis.
    Every function in shared/queue.py looks up `_redis` at call time (not
    as a bound default), so patching the module attribute redirects all of
    them — enqueue, dequeue, retry scheduling, dead-lettering, all of it."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(queue_module, "_redis", fake)
    yield fake
    await fake.flushall()
    await fake.aclose()


@pytest_asyncio.fixture
async def client(fake_redis):
    """Async HTTP client against the real FastAPI app via ASGI transport —
    no real server, no real network socket."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
