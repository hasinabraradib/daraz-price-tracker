import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from shared.config import settings

if os.environ.get("SQLALCHEMY_NULL_POOL"):
    # Test-only: a fresh connection per checkout, never reused across
    # calls. The test suite runs many short-lived event loops, and a
    # pooled connection opened under one loop breaks if reused under a
    # later one ("attached to a different loop"). Set by tests/conftest.py.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
else:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
