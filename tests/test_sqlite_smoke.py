"""SQLite smoke test — NOT a support claim.

pgkeyset is a Postgres library; its suites test Postgres's NULL ordering and typed binds. This
one test walks a listing on SQLite (aiosqlite, a dev dependency only) through a TypeDecorator
that stores UTC and re-attaches it on read, the shape an application uses to run one store on
Postgres or on SQLite. It guards the TypeDecorator path only: deselect it with ``-m "not sqlite"``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from conftest import UTCDateTime
from pgkeyset import Listing, Sort
from pgkeyset.testing import walk

pytest.importorskip("aiosqlite")
pytestmark = pytest.mark.sqlite

_md = sa.MetaData()
_TASK = sa.Table(
    "task",
    _md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.Column("due", UTCDateTime(), nullable=True),
    sa.Column("rank", sa.Integer, nullable=False),
)


@pytest_asyncio.fixture
async def sqlite() -> AsyncIterator[AsyncEngine]:
    built = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with built.begin() as conn:
            await conn.run_sync(_md.create_all)
            t0 = datetime(2026, 1, 1, tzinfo=UTC)
            await conn.execute(
                _TASK.insert(),
                [
                    {
                        "id": f"t{i:02d}",
                        "created_at": t0 + timedelta(minutes=i // 2),
                        "due": None if i % 3 == 0 else t0 + timedelta(days=i),
                        "rank": i % 4,
                    }
                    for i in range(12)
                ],
            )
        yield built
    finally:
        await built.dispose()


@pytest.mark.parametrize(
    "listing",
    [
        Listing("newest", Sort.desc(_TASK.c.created_at), Sort.desc(_TASK.c.id)),
        Listing("due", Sort.asc(_TASK.c.due, nulls="last"), Sort.asc(_TASK.c.id)),
        Listing("rank", Sort.asc(_TASK.c.rank), Sort.desc(_TASK.c.id)),
    ],
    ids=lambda listing: listing.name,
)
async def test_a_decorated_utc_listing_walks_on_sqlite(
    sqlite: AsyncEngine, listing: Listing
) -> None:
    async with sqlite.connect() as conn:
        everything = (await conn.execute(listing.apply(sa.select(_TASK.c.id), None))).all()
    order = [r.id for r in everything]

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        async with sqlite.connect() as conn:
            stmt = listing.apply(sa.select(_TASK), cursor).limit(4)
            page = (await conn.execute(stmt)).mappings().all()
        return listing.paginate(page, 3)

    walked = await walk(fetch, ident=lambda r: r["id"], expected=set(order))
    assert [r["id"] for r in walked] == order
