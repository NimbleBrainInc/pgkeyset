"""Fixtures for the walk suites — REAL Postgres only.

What a listing gets right or wrong is how Postgres orders and compares: where its NULLs sort,
that a comparison against NULL is NULL, and that asyncpg binds a ``timestamptz`` only from a
``datetime``. A fake has none of that, so the walk suites provision a throwaway database on
the superuser Postgres at ``PGKEYSET_TEST_URL``. Without one reachable they **skip**, except
under CI, where a skip would report success having run nothing, so they fail instead. The
codec and declaration suites ask for no database and run anywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Superuser admin connection: creates and drops the throwaway database only. 127.0.0.1, not
# `localhost`: where the resolver prefers ::1, asyncpg can stall its connect timeout first.
_ADMIN_URL = os.environ.get(
    "PGKEYSET_TEST_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:5432/postgres",
)
_DB = f"pgkeyset_test_{uuid.uuid4().hex[:10]}"
_APP_URL = str(sa.engine.make_url(_ADMIN_URL).set(database=_DB))

METADATA = sa.MetaData()

#: The table every walk reads. ``seen_at`` is the nullable sort column, ``band`` an int one.
ITEM = sa.Table(
    "item",
    METADATA,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("status", sa.Text, nullable=False),
)


def _admin_engine() -> AsyncEngine:
    return create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")


async def _admin_reachable() -> bool:
    try:
        engine = _admin_engine()
        try:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except (SQLAlchemyError, OSError):
        return False


async def _provision() -> None:
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f"CREATE DATABASE {_DB}"))
    finally:
        await admin.dispose()
    app = create_async_engine(_APP_URL)
    try:
        async with app.begin() as conn:
            await conn.run_sync(METADATA.create_all)
    finally:
        await app.dispose()


async def _deprovision() -> None:
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)"))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def provisioned_db() -> Iterator[str]:
    if not asyncio.run(_admin_reachable()):
        reason = (
            f"No Postgres reachable at {_ADMIN_URL!r} (PGKEYSET_TEST_URL). The walk suites test "
            "Postgres's own NULL ordering and typed binds."
        )
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(reason)
    asyncio.run(_provision())
    try:
        yield _APP_URL
    finally:
        asyncio.run(_deprovision())


@pytest_asyncio.fixture
async def engine(provisioned_db: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test on an emptied table (an async pool is bound to its loop)."""
    built = create_async_engine(provisioned_db)
    try:
        async with built.begin() as conn:
            await conn.execute(ITEM.delete())
        yield built
    finally:
        await built.dispose()
